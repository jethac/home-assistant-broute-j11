"""Tests for the asynchronous B-route session.

Every test drives the real session against the in-memory adapter in
``tests/fixtures/fake_adapter.py``, so the framing, routing, timeout and
reconnect code under test is the production code.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from custom_components.broute_j11.protocol import commands
from custom_components.broute_j11.protocol.codec import Frame
from custom_components.broute_j11.protocol.echonet import Epc
from custom_components.broute_j11.protocol.session import (
    _ASSOCIATION_ATTEMPTS,
    _RECONNECT_ATTEMPTS,
    _SCAN_ATTEMPTS,
    AuthenticationError,
    BackoffPolicy,
    CachedNetwork,
    J11Session,
    MeterNotFoundError,
    SessionClosedError,
    SessionConfig,
    SessionError,
    SessionTimeoutError,
    TransmissionError,
    scan_budget,
)
from custom_components.broute_j11.protocol.transport import TransportError

from .fixtures import fake_adapter as fake
from .fixtures.fake_adapter import AdapterBehaviour, FakeAdapter

AUTH_ID = "0000000000000000000000000000ABCD"
PASSWORD = "SyntheticPw1"
FAST = {
    "command_timeout": 0.5,
    # The scan deadline follows the dwell time, so the shortest dwell keeps a
    # fruitless scan short (about 0.4 s for all 14 channels).
    "scan_duration": 1,
    "startup_timeout": 0.5,
    "scan_timeout": 1.0,
    "pana_timeout": 0.5,
    "echonet_timeout": 0.5,
}


def make_config(**overrides: object) -> SessionConfig:
    """Build a session config with short timeouts."""
    return SessionConfig(auth_id=AUTH_ID, password=PASSWORD, **{**FAST, **overrides})  # type: ignore[arg-type]


def make_adapter(**overrides: object) -> FakeAdapter:
    """Build a fake adapter that expects the test credentials."""
    return FakeAdapter(
        AdapterBehaviour(auth_id=AUTH_ID, password=PASSWORD, **overrides)  # type: ignore[arg-type]
    )


class MemoryCache:
    """An in-memory :class:`NetworkCache` for the reconnect tests."""

    def __init__(self, network: CachedNetwork | None) -> None:
        """Start with ``network`` already cached."""
        self.network = network

    def load(self) -> CachedNetwork | None:
        """Return the cached network."""
        return self.network

    def store(self, network: CachedNetwork) -> None:
        """Remember ``network``."""
        self.network = network


def make_session(
    adapter: FakeAdapter, config: SessionConfig | None = None, **kwargs: object
) -> J11Session:
    """Build a session bound to ``adapter``."""
    return J11Session(adapter, config or make_config(), **kwargs)  # type: ignore[arg-type]


async def test_connect_runs_the_documented_start_up_sequence() -> None:
    adapter = make_adapter()
    session = make_session(adapter)
    link = await session.async_connect()
    try:
        assert [frame.command_code for frame in adapter.requests] == [
            commands.CommandCode.RESET,
            commands.CommandCode.GET_VERSION,
            commands.CommandCode.SET_INITIAL_SETTINGS,
            commands.CommandCode.SET_CREDENTIALS,
            commands.CommandCode.ACTIVE_SCAN,
            commands.CommandCode.SET_INITIAL_SETTINGS,
            commands.CommandCode.START_ROUTE_B,
            commands.CommandCode.OPEN_UDP_PORT,
            commands.CommandCode.START_PANA,
            commands.CommandCode.TRANSMIT_DATA,
            commands.CommandCode.TRANSMIT_DATA,
        ]
        assert link.channel == fake.METER_CHANNEL
        assert link.pan_id == fake.METER_PAN_ID
        assert link.mac_address == fake.METER_MAC
        assert link.firmware_version == "1.2.3"
        assert session.connected
        assert session.stats.connects == 1
    finally:
        await session.async_close()


async def test_connect_rescans_on_the_channel_the_beacon_reported() -> None:
    adapter = make_adapter()
    session = make_session(adapter)
    await session.async_connect()
    try:
        settings = adapter.sent(commands.CommandCode.SET_INITIAL_SETTINGS)
        assert settings[0].data[2] == commands.MIN_CHANNEL
        assert settings[1].data[2] == fake.METER_CHANNEL
    finally:
        await session.async_close()


async def test_connect_reads_the_meter_profile() -> None:
    adapter = make_adapter()
    session = make_session(adapter)
    await session.async_connect()
    try:
        profile = session.profile
        assert profile.coefficient == 1
        assert profile.unit == Decimal("0.1")
        assert profile.digits == 6
        assert profile.manufacturer_code == "0x00000B"
        assert profile.standard_version == "0x00004600"
        assert profile.serial_number == "SYNTHETIC001"
    finally:
        await session.async_close()


async def test_connect_is_idempotent() -> None:
    adapter = make_adapter()
    session = make_session(adapter)
    first = await session.async_connect()
    try:
        assert await session.async_connect() is first
        assert adapter.opens == 1
    finally:
        await session.async_close()


async def test_read_meter_converts_every_measurement() -> None:
    adapter = make_adapter()
    session = make_session(adapter)
    async with session:
        reading = await session.async_read_meter()
    assert reading.instantaneous_power == fake.EXPECTED_POWER
    assert reading.current_r_phase == fake.EXPECTED_CURRENT_R
    assert reading.current_t_phase == fake.EXPECTED_CURRENT_T
    assert reading.cumulative_forward_energy == fake.EXPECTED_FORWARD_KWH
    assert reading.cumulative_reverse_energy == fake.EXPECTED_REVERSE_KWH


async def test_read_meter_reports_a_single_phase_meter() -> None:
    properties = dict(fake.DEFAULT_PROPERTIES)
    properties[Epc.INSTANTANEOUS_CURRENT] = (56).to_bytes(2, "big") + b"\x7f\xfe"
    adapter = make_adapter(properties=properties)
    session = make_session(adapter)
    async with session:
        reading = await session.async_read_meter()
    assert reading.current_r_phase == fake.EXPECTED_CURRENT_R
    assert reading.current_t_phase is None


async def test_read_meter_applies_the_coefficient() -> None:
    properties = dict(fake.DEFAULT_PROPERTIES)
    properties[Epc.COEFFICIENT] = (10).to_bytes(4, "big")
    adapter = make_adapter(properties=properties)
    session = make_session(adapter)
    async with session:
        reading = await session.async_read_meter()
    assert reading.cumulative_forward_energy == fake.EXPECTED_FORWARD_KWH * 10


async def test_read_meter_skips_a_spontaneous_notification() -> None:
    adapter = make_adapter(send_instance_list=True)
    session = make_session(adapter)
    async with session:
        reading = await session.async_read_meter()
    assert reading.instantaneous_power == fake.EXPECTED_POWER
    assert session.stats.unsolicited_datagrams >= 1


async def test_read_meter_retries_a_dropped_meter_response() -> None:
    adapter = make_adapter(drop_meter_responses=1)
    config = make_config()
    session = make_session(adapter, config)
    async with session:
        reading = await session.async_read_meter()
    assert reading.instantaneous_power == fake.EXPECTED_POWER
    assert session.stats.echonet_retries >= 1


async def test_read_meter_gives_up_after_the_configured_attempts() -> None:
    adapter = make_adapter(drop_meter_responses=99)
    session = make_session(adapter, make_config(echonet_attempts=2))
    async with session:
        with pytest.raises(SessionTimeoutError):
            await session.async_read_meter()
    assert session.stats.timeouts >= 2


async def test_read_meter_rebuilds_the_session_after_exhausted_no_ack() -> None:
    adapter = make_adapter()
    session = make_session(adapter, make_config(echonet_attempts=2))
    await session.async_connect()
    try:
        adapter.behaviour.transmit_no_ack = 2
        reading = await session.async_read_meter()
        assert reading.instantaneous_power == fake.EXPECTED_POWER
        assert adapter.opens == 2
        assert adapter.closes == 1
        assert session.stats.connects == 2
        assert session.stats.reconnects == 1
        assert len(adapter.sent(commands.CommandCode.ACTIVE_SCAN)) == 1
    finally:
        await session.async_close()


async def test_read_meter_rebuilds_the_session_after_silent_transmit_command() -> None:
    adapter = make_adapter()
    session = make_session(
        adapter,
        make_config(command_timeout=0.05, echonet_attempts=2),
    )
    await session.async_connect()
    try:
        adapter.behaviour.silent_command_responses[
            commands.CommandCode.TRANSMIT_DATA
        ] = 2
        reading = await session.async_read_meter()
        assert reading.instantaneous_power == fake.EXPECTED_POWER
        assert adapter.opens == 2
        assert adapter.closes == 1
        assert session.stats.connects == 2
        assert session.stats.reconnects == 1
        assert len(adapter.sent(commands.CommandCode.ACTIVE_SCAN)) == 1
    finally:
        await session.async_close()


async def test_failed_recovery_read_leaves_the_session_disconnected() -> None:
    adapter = make_adapter()
    session = make_session(adapter, make_config(echonet_attempts=2))
    await session.async_connect()
    try:
        # Two failures exhaust the original read, two are consumed by the
        # reconnect's best-effort profile read, and two exhaust recovery.
        adapter.behaviour.transmit_no_ack = 6
        with pytest.raises(TransmissionError):
            await session.async_read_meter()

        assert not session.connected
        assert adapter.opens == 2
        assert adapter.closes == 2
        assert session.stats.reconnects == 1
        assert len(adapter.sent(commands.CommandCode.ACTIVE_SCAN)) == 1

        reading = await session.async_read_meter()
        assert reading.instantaneous_power == fake.EXPECTED_POWER
        assert adapter.opens == 3
        assert session.stats.connects == 3
        assert len(adapter.sent(commands.CommandCode.ACTIVE_SCAN)) == 1
    finally:
        await session.async_close()


async def test_a_refused_get_is_reported() -> None:
    adapter = make_adapter(get_sna=True)
    session = make_session(adapter, make_config(echonet_attempts=1))
    async with session:
        with pytest.raises(SessionError):
            await session.async_read_meter()


async def test_a_meter_that_hides_its_profile_still_connects() -> None:
    adapter = make_adapter(get_sna=True)
    session = make_session(adapter, make_config(echonet_attempts=1))
    async with session:
        assert session.connected
        assert session.profile.coefficient == 1
        assert session.profile.serial_number is None


async def test_a_silent_adapter_times_out_without_hanging() -> None:
    adapter = make_adapter(silent_commands={commands.CommandCode.GET_VERSION})
    session = make_session(adapter)
    with pytest.raises(SessionTimeoutError):
        await session.async_connect()
    assert adapter.closes == 1


async def test_a_missing_startup_notification_times_out() -> None:
    adapter = make_adapter(
        silent_notifications={commands.NotificationCode.STARTUP_COMPLETED}
    )
    session = make_session(adapter)
    with pytest.raises(SessionTimeoutError):
        await session.async_connect()


async def test_a_rejected_command_is_reported_with_its_result() -> None:
    adapter = make_adapter(credentials_result=0x04)
    session = make_session(adapter)
    with pytest.raises(commands.CommandFailedError) as excinfo:
        await session.async_connect()
    assert excinfo.value.result == 0x04


async def test_credentials_already_stored_is_not_an_error() -> None:
    adapter = make_adapter(credentials_result=0x58)
    session = make_session(adapter)
    async with session:
        assert session.connected


async def test_no_beacon_response_is_reported_as_a_missing_meter() -> None:
    adapter = make_adapter(beacon_channel=None)
    session = make_session(adapter)
    with pytest.raises(MeterNotFoundError):
        await session.async_connect()


async def test_a_silent_scan_times_out_as_a_missing_meter() -> None:
    adapter = make_adapter(
        silent_notifications={commands.NotificationCode.ACTIVE_SCAN_RESULT}
    )
    session = make_session(adapter, make_config(scan_timeout=0.3))
    with pytest.raises(MeterNotFoundError):
        await session.async_connect()


async def test_a_rejected_pana_authentication_latches_the_session() -> None:
    adapter = make_adapter(pana_result=0x02)
    session = make_session(adapter)
    with pytest.raises(AuthenticationError):
        await session.async_connect()
    assert session.authentication_failed
    requests = len(adapter.requests)
    with pytest.raises(AuthenticationError):
        await session.async_ensure_connected()
    assert len(adapter.requests) == requests, "a latched session must not retry"


async def test_an_unanswered_pana_exchange_stays_retryable() -> None:
    adapter = make_adapter(pana_result=0x03)
    session = make_session(adapter)
    with pytest.raises(SessionError) as excinfo:
        await session.async_connect()
    assert not isinstance(excinfo.value, AuthenticationError)
    assert not session.authentication_failed


async def test_the_scan_is_retried_with_a_longer_dwell() -> None:
    adapter = make_adapter(silent_scans=1)
    config = make_config()
    session = make_session(adapter, config)
    async with session:
        assert session.connected
    durations = [
        frame.data[0] for frame in adapter.sent(commands.CommandCode.ACTIVE_SCAN)
    ]
    assert durations == [config.scan_duration, config.scan_duration + 1]


async def test_the_scan_retries_are_bounded() -> None:
    adapter = make_adapter(silent_scans=_SCAN_ATTEMPTS)
    session = make_session(adapter)
    with pytest.raises(MeterNotFoundError):
        await session.async_connect()
    assert len(adapter.sent(commands.CommandCode.ACTIVE_SCAN)) == _SCAN_ATTEMPTS


def test_the_scan_budget_follows_the_dwell_time_and_the_channels() -> None:
    mask = commands.ALL_CHANNELS_MASK
    # 9.64 ms * 2**9 per channel over 14 channels is about 69 s of radio time,
    # which a fixed one-minute deadline would cut short.
    assert scan_budget(9, mask) > 69.0
    assert scan_budget(10, mask) == pytest.approx(2 * scan_budget(9, mask))
    assert scan_budget(9, 1 << fake.METER_CHANNEL) == pytest.approx(
        scan_budget(9, mask) / 14
    )


async def test_a_scan_that_dwells_on_every_channel_is_not_cut_short() -> None:
    """The deadline must outlast a scan that reports channels in real time."""
    adapter = make_adapter(dwell=True)
    session = make_session(adapter, make_config(scan_duration=2))
    async with session:
        assert session.connected
    assert len(adapter.sent(commands.CommandCode.ACTIVE_SCAN)) == 1


async def test_the_scan_request_waits_for_the_whole_dwell() -> None:
    """The request wait is the dwell budget, not the configured scan timeout.

    The adapter answers 0x0051 when the scan has finished rather than when it
    starts, so a duration-9 scan across every channel answers after about 69 s
    and the default one-minute timeout would fail it. The fake dwells at a
    shorter duration to keep the test quick, with a scan timeout well below the
    dwell so a fixed request timeout cannot pass.
    """
    default = SessionConfig(auth_id=AUTH_ID, password=PASSWORD)
    assert scan_budget(9, default.channel_mask) > default.scan_timeout
    adapter = make_adapter(dwell=True)
    config = make_config(scan_duration=3, scan_timeout=0.05, command_timeout=0.05)
    session = make_session(adapter, config)
    async with session:
        assert session.connected
    assert len(adapter.sent(commands.CommandCode.ACTIVE_SCAN)) == 1


async def test_a_failed_association_is_retried_from_a_fresh_scan() -> None:
    adapter = make_adapter(association_failures=1)
    session = make_session(adapter)
    async with session:
        assert session.connected
    assert len(adapter.sent(commands.CommandCode.START_ROUTE_B)) == 2
    assert len(adapter.sent(commands.CommandCode.ACTIVE_SCAN)) == 2


async def test_association_retries_are_bounded() -> None:
    adapter = make_adapter(association_failures=_ASSOCIATION_ATTEMPTS)
    session = make_session(adapter)
    with pytest.raises(commands.CommandFailedError):
        await session.async_connect()
    assert (
        len(adapter.sent(commands.CommandCode.START_ROUTE_B)) == _ASSOCIATION_ATTEMPTS
    )


async def test_an_unacknowledged_transmission_is_retried() -> None:
    adapter = make_adapter()
    session = make_session(adapter)
    async with session:
        adapter.behaviour.transmit_no_ack = 1
        reading = await session.async_read_meter()
    assert reading.instantaneous_power == fake.EXPECTED_POWER
    assert session.stats.echonet_retries == 1


async def test_a_refused_identity_property_keeps_the_scaling_values() -> None:
    adapter = make_adapter(
        unsupported={Epc.SERIAL_NUMBER, Epc.MANUFACTURER_CODE, Epc.STANDARD_VERSION}
    )
    session = make_session(adapter, make_config(echonet_attempts=1))
    async with session:
        profile = session.profile
    assert profile.unit == Decimal("0.1")
    assert profile.digits == 6
    assert profile.serial_number is None
    assert profile.manufacturer_code is None


async def test_a_cached_network_is_rejoined_without_scanning() -> None:
    adapter = make_adapter()
    cache = MemoryCache(
        CachedNetwork(
            channel=fake.METER_CHANNEL,
            pan_id=fake.METER_PAN_ID,
            mac_address=fake.METER_MAC,
        )
    )
    session = make_session(adapter, network_cache=cache)
    async with session:
        assert session.connected
    assert not adapter.sent(commands.CommandCode.ACTIVE_SCAN)
    settings = adapter.sent(commands.CommandCode.SET_INITIAL_SETTINGS)
    assert [frame.data[2] for frame in settings] == [fake.METER_CHANNEL]


async def test_a_stale_cached_network_falls_back_to_a_scan() -> None:
    adapter = make_adapter(route_b_failures=1)
    cache = MemoryCache(
        CachedNetwork(
            channel=commands.MIN_CHANNEL,
            pan_id=fake.METER_PAN_ID,
            mac_address=fake.METER_MAC,
        )
    )
    session = make_session(adapter, network_cache=cache)
    async with session:
        assert session.connected
    assert adapter.sent(commands.CommandCode.ACTIVE_SCAN)
    assert cache.network == CachedNetwork(
        channel=fake.METER_CHANNEL,
        pan_id=fake.METER_PAN_ID,
        mac_address=fake.METER_MAC,
    )


async def test_the_joined_network_is_cached_for_the_next_connect() -> None:
    adapter = make_adapter()
    cache = MemoryCache(None)
    session = make_session(adapter, network_cache=cache)
    async with session:
        assert session.connected
    assert cache.network is not None
    assert cache.network.channel == fake.METER_CHANNEL


async def test_a_missing_pana_notification_times_out() -> None:
    adapter = make_adapter(silent_notifications={commands.NotificationCode.PANA_RESULT})
    session = make_session(adapter)
    with pytest.raises(SessionTimeoutError):
        await session.async_connect()
    assert not session.authentication_failed


async def test_an_unopenable_port_is_reported() -> None:
    adapter = FakeAdapter(AdapterBehaviour(fail_open=True))
    session = make_session(adapter)
    with pytest.raises(TransportError):
        await session.async_connect()


async def test_line_noise_before_the_first_frame_is_ignored() -> None:
    adapter = make_adapter(line_noise=b"\x00\x01\x02\xff\xfe")
    session = make_session(adapter)
    async with session:
        assert session.connected
    assert session.stats.frames.discarded_bytes >= 5


async def test_losing_the_usb_device_disconnects_the_session() -> None:
    adapter = make_adapter()
    lost = asyncio.Event()
    session = make_session(adapter, on_disconnect=lost.set)
    await session.async_connect()
    adapter.behaviour.fail_read_after = adapter.reads
    async with asyncio.timeout(2):
        await lost.wait()
    assert not session.connected
    await session.async_close()


async def test_a_write_to_a_lost_device_reports_a_closed_session() -> None:
    adapter = make_adapter()
    session = make_session(adapter)
    await session.async_connect()
    adapter.close()
    with pytest.raises(SessionClosedError):
        await session.async_read_meter()
    await session.async_close()


async def test_a_radio_disconnection_marks_the_session_disconnected() -> None:
    adapter = make_adapter()
    lost = asyncio.Event()
    session = make_session(adapter, on_disconnect=lost.set)
    await session.async_connect()
    adapter.emit_connection_lost()
    async with asyncio.timeout(2):
        await lost.wait()
    assert not session.connected
    await session.async_close()


async def test_ensure_connected_rebuilds_a_lost_link() -> None:
    adapter = make_adapter()
    lost = asyncio.Event()
    session = make_session(
        adapter,
        backoff=BackoffPolicy(initial=0.01, maximum=0.02, jitter=0.0),
        on_disconnect=lost.set,
    )
    await session.async_connect()
    adapter.emit_connection_lost()
    async with asyncio.timeout(2):
        await lost.wait()
    reading = await session.async_read_meter()
    assert reading.instantaneous_power == fake.EXPECTED_POWER
    assert session.stats.connects == 2
    await session.async_close()


async def test_ensure_connected_stops_after_the_last_attempt() -> None:
    adapter = make_adapter(beacon_channel=None)
    session = make_session(
        adapter,
        make_config(scan_timeout=0.1),
        backoff=BackoffPolicy(initial=0.01, maximum=0.01, jitter=0.0),
    )
    with pytest.raises(SessionClosedError):
        await session.async_ensure_connected()
    scans = adapter.sent(commands.CommandCode.ACTIVE_SCAN)
    assert len(scans) == _RECONNECT_ATTEMPTS * _SCAN_ATTEMPTS


async def test_closing_terminates_pana_and_releases_the_port() -> None:
    adapter = make_adapter()
    session = make_session(adapter)
    await session.async_connect()
    await session.async_close()
    assert adapter.sent(commands.CommandCode.TERMINATE_PANA)
    assert adapter.closes == 1
    assert not session.connected


async def test_closing_an_unconnected_session_is_harmless() -> None:
    adapter = make_adapter()
    session = make_session(adapter)
    await session.async_close()
    assert adapter.closes == 0


async def test_closing_while_the_device_is_gone_does_not_raise() -> None:
    adapter = make_adapter()
    session = make_session(adapter)
    await session.async_connect()
    adapter.behaviour.fail_read_after = 0
    adapter.behaviour.silent_commands = {commands.CommandCode.TERMINATE_PANA}
    await session.async_close()
    assert not session.connected


async def test_reads_are_cancelled_promptly_on_close() -> None:
    adapter = make_adapter()
    session = make_session(adapter)
    await session.async_connect()
    async with asyncio.timeout(2):
        await session.async_close()


async def test_a_late_response_is_discarded_without_disturbing_the_session() -> None:
    adapter = make_adapter()
    session = make_session(adapter)
    async with session:
        adapter.respond(commands.CommandCode.GET_VERSION, bytes([0x01]) + bytes(8))
        await asyncio.sleep(0.05)
        assert session.connected


async def test_an_undecodable_notification_is_ignored() -> None:
    adapter = make_adapter()
    session = make_session(adapter)
    async with session:
        adapter.notify(commands.NotificationCode.CONNECTION_STATUS, b"\x04")
        adapter.notify(commands.NotificationCode.PACKET_RECEPTION_FAILURE, b"\x01")
        await asyncio.sleep(0.05)
        assert session.connected


async def test_a_reception_failure_notification_is_counted() -> None:
    adapter = make_adapter()
    session = make_session(adapter)
    async with session:
        adapter.notify(
            commands.NotificationCode.PACKET_RECEPTION_FAILURE,
            bytes([0x01]) + bytes(16) + bytes([0x01, 0x01, 0x00, 0x00]),
        )
        await asyncio.sleep(0.05)
        assert session.connected


async def test_an_unknown_frame_category_is_ignored() -> None:
    adapter = make_adapter()
    session = make_session(adapter)
    async with session:
        adapter.emit(Frame(command_code=0x00D9, data=b"").encode())
        await asyncio.sleep(0.05)
        assert session.connected


async def test_reading_without_a_connection_reconnects() -> None:
    adapter = make_adapter()
    session = make_session(adapter)
    reading = await session.async_read_meter()
    assert reading.instantaneous_power == fake.EXPECTED_POWER
    await session.async_close()


def test_malformed_credentials_are_rejected_before_any_io() -> None:
    with pytest.raises(commands.CredentialFormatError):
        SessionConfig(auth_id="too-short", password=PASSWORD)
    with pytest.raises(commands.CredentialFormatError):
        SessionConfig(auth_id=AUTH_ID, password="short")


def test_backoff_grows_geometrically_and_is_capped() -> None:
    policy = BackoffPolicy(initial=5.0, maximum=60.0, factor=2.0, jitter=0.0)
    assert policy.delay(1, random_value=0.5) == 5.0
    assert policy.delay(2, random_value=0.5) == 10.0
    assert policy.delay(10, random_value=0.5) == 60.0


def test_backoff_jitter_stays_within_its_band() -> None:
    policy = BackoffPolicy(initial=10.0, maximum=100.0, jitter=0.25)
    assert policy.delay(1, random_value=0.0) == 7.5
    assert policy.delay(1, random_value=1.0) == 12.5
    assert 7.5 <= policy.delay(1) <= 12.5


def test_backoff_rejects_a_zero_attempt() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        BackoffPolicy().delay(0)
