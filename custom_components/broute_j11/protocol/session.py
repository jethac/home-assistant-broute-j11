"""Asynchronous B-route session on top of the J11 command layer.

The session owns the adapter: it runs every blocking transport call on a
dedicated executor, serialises UART transactions, routes responses back to the
caller that asked for them, dispatches notifications, and rebuilds the whole
B-route/PANA connection after a failure with capped exponential backoff.

Two rules shape the design:

* Nothing here may block Home Assistant's event loop. Reads happen in an
  executor with a bounded per-call timeout, so cancellation and shutdown take
  effect within one read slice.
* A rejected credential is not a transient failure. Authentication failures
  latch the session so a coordinator cannot hammer the meter, which would lock
  the account out; the config entry has to be reloaded once the credentials are
  corrected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
import contextlib
from dataclasses import dataclass, field
from decimal import Decimal
from ipaddress import IPv6Address
import logging
import random
from types import TracebackType
from typing import Final, Protocol, Self, runtime_checkable

from . import commands
from .codec import Frame, FrameReassembler, FrameStats, ProtocolError, response_code
from .echonet import (
    LOW_VOLTAGE_METER_OBJECT,
    EchonetFrameError,
    EchonetLiteFrame,
    Epc,
    Esv,
    MeterProfile,
    decode_coefficient,
    decode_cumulative_digits,
    decode_cumulative_energy,
    decode_instantaneous_current,
    decode_instantaneous_power,
    decode_manufacturer_code,
    decode_protocol_version,
    decode_serial_number,
    encode_get,
    unit_multiplier,
)
from .transport import READ_CHUNK, ByteTransport, TransportError

_LOGGER = logging.getLogger(__name__)

#: Channel used for the initial settings that precede an active scan; the real
#: channel is only known once the meter's beacon has been received.
PROVISIONAL_CHANNEL: Final = commands.MIN_CHANNEL
#: Per-channel dwell time exponent for the active scan (about 2.5 s). Shorter
#: dwells miss the meter's beacon on real installations.
DEFAULT_SCAN_DURATION: Final = 8
#: How much longer each retried scan dwells per channel.
_SCAN_DURATION_STEP: Final = 1
#: Highest dwell time exponent the adapter accepts (specification §3.2.3.4).
_MAX_SCAN_DURATION: Final = 14
#: How many times a fruitless scan is repeated with a longer dwell.
_SCAN_ATTEMPTS: Final = 3
#: Dwell time unit of the active scan: a channel is listened to for
#: ``9.64 ms * 2**duration`` (specification §3.2.3.4).
_DWELL_UNIT: Final = 0.00964
#: Share of the dwell budget added so a busy adapter can still finish.
_SCAN_MARGIN: Final = 0.5
#: How many times an association failure restarts the whole join sequence.
_ASSOCIATION_ATTEMPTS: Final = 3
#: Highest transaction ID before wrapping.
_MAX_TRANSACTION_ID: Final = 0xFFFF

#: Properties fetched once per connection to scale the energy counters. They are
#: requested on their own so a meter that refuses an optional identity property
#: cannot cost us the scaling factors.
SCALING_PROPERTIES: Final[tuple[Epc, ...]] = (
    Epc.COEFFICIENT,
    Epc.CUMULATIVE_DIGITS,
    Epc.CUMULATIVE_UNIT,
)

#: Optional properties that only describe the meter.
IDENTITY_PROPERTIES: Final[tuple[Epc, ...]] = (
    Epc.MANUFACTURER_CODE,
    Epc.STANDARD_VERSION,
    Epc.SERIAL_NUMBER,
)

#: Properties polled on every refresh.
MEASUREMENT_PROPERTIES: Final[tuple[Epc, ...]] = (
    Epc.INSTANTANEOUS_POWER,
    Epc.INSTANTANEOUS_CURRENT,
    Epc.CUMULATIVE_FORWARD_ENERGY,
    Epc.CUMULATIVE_REVERSE_ENERGY,
)


class SessionError(ProtocolError):
    """The session could not carry out an operation."""


class SessionClosedError(SessionError):
    """The session is not connected, or its link disappeared mid-operation."""


class SessionTimeoutError(SessionError):
    """The adapter or the meter did not answer in time."""


class AuthenticationError(SessionError):
    """PANA authentication was rejected; the credentials need to change."""


class MeterNotFoundError(SessionError):
    """No smart meter answered the active scan."""


class TransmissionError(SessionError):
    """The adapter could not deliver a datagram, but a retry may work."""


def scan_budget(duration: int, channel_mask: int) -> float:
    """Return how long an active scan at ``duration`` needs, with margin.

    The adapter dwells ``9.64 ms * 2**duration`` on every channel in the mask
    before it reports the last result, so a fixed timeout silently truncates a
    longer retry: 14 channels at duration 9 already take about 69 s.
    """
    channels = len(_channels_in_mask(channel_mask)) or 1
    return channels * _DWELL_UNIT * 2.0**duration * (1 + _SCAN_MARGIN)


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Everything a session needs to reach one meter.

    ``auth_id`` and ``password`` are secrets: they are validated on
    construction and never logged.
    """

    auth_id: str
    password: str
    scan_duration: int = DEFAULT_SCAN_DURATION
    channel_mask: int = commands.ALL_CHANNELS_MASK
    command_timeout: float = 5.0
    startup_timeout: float = 15.0
    #: How long the adapter may take to acknowledge the scan command itself.
    #: How long the scan then runs follows from the dwell time and the channel
    #: mask, see :func:`scan_budget`.
    scan_timeout: float = 60.0
    pana_timeout: float = 45.0
    echonet_timeout: float = 15.0
    echonet_attempts: int = 3

    def __post_init__(self) -> None:
        """Reject malformed credentials before any hardware is touched."""
        commands.validate_auth_id(self.auth_id)
        commands.validate_password(self.password)


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Capped exponential backoff with jitter for reconnect attempts."""

    initial: float = 5.0
    maximum: float = 300.0
    factor: float = 2.0
    jitter: float = 0.25

    def delay(self, attempt: int, *, random_value: float | None = None) -> float:
        """Return how long to wait before ``attempt`` (1 is the first retry)."""
        if attempt < 1:
            raise ValueError(f"attempt must be at least 1, got {attempt}")
        base = min(self.initial * self.factor ** (attempt - 1), self.maximum)
        spread = random_value if random_value is not None else random.random()
        return base * (1 - self.jitter + 2 * self.jitter * spread)


@dataclass(frozen=True, slots=True)
class CachedNetwork:
    """The minimum state needed to rejoin a known meter without scanning.

    ``mac_address`` is a stable private identifier, so callers must redact it
    before it reaches diagnostics (PRD §6.2, §6.7).
    """

    channel: int
    pan_id: int
    mac_address: bytes


@runtime_checkable
class NetworkCache(Protocol):
    """Storage for the network state a session rejoins after an interruption."""

    def load(self) -> CachedNetwork | None:
        """Return the last joined network, or ``None`` if none is known."""

    def store(self, network: CachedNetwork) -> None:
        """Remember ``network`` for the next session."""


@dataclass(frozen=True, slots=True)
class MeterLink:
    """The radio link this session established.

    ``mac_address`` and ``pan_id`` identify a specific meter, so callers must
    redact them before they reach diagnostics.
    """

    channel: int
    pan_id: int
    mac_address: bytes
    address: IPv6Address
    rssi: int
    firmware_version: str


@dataclass(slots=True)
class SessionStats:
    """Counters for diagnostics; nothing here identifies a device."""

    connects: int = 0
    reconnects: int = 0
    requests: int = 0
    timeouts: int = 0
    echonet_retries: int = 0
    notifications: int = 0
    unsolicited_datagrams: int = 0
    frames: FrameStats = field(default_factory=FrameStats)


@dataclass(frozen=True, slots=True)
class MeterReading:
    """One poll of the meter's measurement properties."""

    instantaneous_power: int | None
    current_r_phase: Decimal | None
    current_t_phase: Decimal | None
    cumulative_forward_energy: Decimal | None
    cumulative_reverse_energy: Decimal | None


class J11Session:
    """A connected, self-healing B-route session with one smart meter."""

    def __init__(
        self,
        transport: ByteTransport,
        config: SessionConfig,
        *,
        backoff: BackoffPolicy | None = None,
        on_disconnect: Callable[[], None] | None = None,
        network_cache: NetworkCache | None = None,
    ) -> None:
        """Create a session; no I/O happens until :meth:`async_connect`."""
        self._transport = transport
        self._config = config
        self._backoff = backoff or BackoffPolicy()
        self._on_disconnect = on_disconnect
        self._network_cache = network_cache
        self._cached_network = (
            network_cache.load() if network_cache is not None else None
        )
        self._reassembler = FrameReassembler()
        self._stats = SessionStats()
        self._transaction_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[Frame]] = {}
        self._notifications: dict[int, asyncio.Queue[Frame]] = {}
        self._link_lost = asyncio.Event()
        self._datagrams: asyncio.Queue[commands.UdpDatagram] = asyncio.Queue()
        self._scan_results: asyncio.Queue[commands.ActiveScanResult] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._link: MeterLink | None = None
        self._profile = MeterProfile()
        self._transaction_id = 0
        self._closing = False
        self._authentication_failed = False
        self._transport_open = False

    @property
    def connected(self) -> bool:
        """Whether a PANA session is currently established."""
        return self._link is not None

    @property
    def cached_network(self) -> CachedNetwork | None:
        """The network state a later session can rejoin without scanning."""
        return self._cached_network

    @property
    def link(self) -> MeterLink | None:
        """The established radio link, if any."""
        return self._link

    @property
    def profile(self) -> MeterProfile:
        """Scaling and identity properties read from the meter."""
        return self._profile

    @property
    def stats(self) -> SessionStats:
        """Counters describing this session's traffic."""
        return self._stats

    @property
    def authentication_failed(self) -> bool:
        """Whether the meter rejected these credentials.

        Once set, the session refuses to reconnect until it is recreated.
        """
        return self._authentication_failed

    async def __aenter__(self) -> Self:
        """Connect and return the session."""
        await self.async_connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the session."""
        await self.async_close()

    async def async_connect(self) -> MeterLink:
        """Bring up the serial link, the radio link and the PANA session."""
        async with self._connect_lock:
            if self._link is not None:
                return self._link
            if self._authentication_failed:
                raise AuthenticationError(
                    "the meter rejected these Route-B credentials; "
                    "correct them and reload the integration"
                )
            self._closing = False
            self._link_lost.clear()
            await self._async_open_transport()
            try:
                link = await self._async_establish()
            except BaseException:
                await self._async_teardown()
                raise
            self._link = link
            self._stats.connects += 1
            return link

    async def async_close(self) -> None:
        """Terminate PANA if possible and release the serial port."""
        self._closing = True
        if self._link is not None:
            try:
                await self._async_request(
                    commands.terminate_pana_request(),
                    commands.CommandCode.TERMINATE_PANA,
                )
            except (SessionError, ProtocolError, TransportError) as err:
                _LOGGER.debug("Terminating PANA on shutdown failed: %s", err)
        await self._async_teardown()

    async def async_read_meter(self) -> MeterReading:
        """Poll the meter, reconnecting first if the link went away."""
        await self.async_ensure_connected()
        values = await self._async_get_properties(MEASUREMENT_PROPERTIES)
        return self._decode_reading(values)

    async def async_ensure_connected(self) -> MeterLink:
        """Return a live link, rebuilding it with backoff if necessary."""
        if self._link is not None:
            return self._link
        attempt = 0
        while True:
            attempt += 1
            try:
                link = await self.async_connect()
            except AuthenticationError:
                raise
            except (SessionError, ProtocolError, TransportError) as err:
                if attempt >= _RECONNECT_ATTEMPTS:
                    raise SessionClosedError(
                        f"could not re-establish the B-route session: {err}"
                    ) from err
                delay = self._backoff.delay(attempt)
                _LOGGER.debug(
                    "Reconnect attempt %s failed (%s); retrying in %.1fs",
                    attempt,
                    err,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                if attempt > 1:
                    self._stats.reconnects += 1
                return link

    async def _async_open_transport(self) -> None:
        loop = asyncio.get_running_loop()
        self._reassembler.reset()
        await loop.run_in_executor(None, self._transport.open)
        self._transport_open = True
        self._reader = loop.create_task(self._async_read_loop())

    async def _async_teardown(self) -> None:
        self._link = None
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        if self._transport_open:
            self._transport_open = False
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._transport.close)
        self._fail_waiters(SessionClosedError("the session was closed"))

    async def _async_establish(self) -> MeterLink:
        """Rejoin the cached network if there is one, else scan for the meter.

        A full active scan costs a minute of radio time, so a serial or radio
        interruption reuses the channel the meter was last found on and only
        falls back to scanning when that channel no longer answers (PRD §6.2).
        """
        cached = self._cached_network
        if cached is None:
            return await self._async_associate(None)
        try:
            return await self._async_associate(cached)
        except AuthenticationError:
            raise
        except (SessionError, ProtocolError) as err:
            _LOGGER.info(
                "Rejoining the meter on channel %s failed (%s); scanning again",
                cached.channel,
                err,
            )
            self._cached_network = None
        return await self._async_associate(None)

    async def _async_associate(self, cached: CachedNetwork | None) -> MeterLink:
        """Join the meter, restarting the sequence after an association failure.

        A meter that answered the scan still refuses the first MAC association
        often enough that one failure must not fail the whole setup: the adapter
        reports result 0x0E, and the next attempt from a reset module (and a
        fresh scan when there is no cached channel) usually succeeds.
        """
        for attempt in range(1, _ASSOCIATION_ATTEMPTS + 1):
            try:
                return await self._async_establish_network(cached)
            except commands.CommandFailedError as err:
                if (
                    err.result != _MAC_CONNECTION_FAILED
                    or attempt == _ASSOCIATION_ATTEMPTS
                ):
                    raise
                _LOGGER.info(
                    "Associating with the meter failed (%s); retrying (%s/%s)",
                    err,
                    attempt + 1,
                    _ASSOCIATION_ATTEMPTS,
                )
        raise SessionError("the meter refused every association attempt")

    async def _async_establish_network(self, cached: CachedNetwork | None) -> MeterLink:
        """Run the documented Route-B start-up sequence (B-route note §3)."""
        await self._async_reset()
        version = commands.parse_version_response(
            await self._async_request(
                commands.version_request(), commands.CommandCode.GET_VERSION
            )
        )
        if cached is not None:
            await self._async_initial_settings(cached.channel)
            await self._async_set_credentials()
        else:
            await self._async_initial_settings(PROVISIONAL_CHANNEL)
            await self._async_set_credentials()
            beacon = await self._async_scan()
            await self._async_initial_settings(beacon.channel)
        start = commands.parse_route_b_start_response(
            await self._async_request(
                commands.start_route_b_request(),
                commands.CommandCode.START_ROUTE_B,
                timeout=self._config.pana_timeout,
            )
        )
        if cached is not None and start.mac_address != cached.mac_address:
            raise SessionError("the cached channel belongs to a different meter")
        await self._async_request(
            commands.open_udp_port_request(), commands.CommandCode.OPEN_UDP_PORT
        )
        await self._async_start_pana(start.mac_address)
        self._remember_network(
            CachedNetwork(
                channel=start.channel,
                pan_id=start.pan_id,
                mac_address=start.mac_address,
            )
        )
        link = MeterLink(
            channel=start.channel,
            pan_id=start.pan_id,
            mac_address=start.mac_address,
            address=start.address,
            rssi=start.rssi,
            firmware_version=str(version),
        )
        self._link = link
        try:
            self._profile = await self._async_read_profile()
        except (SessionError, ProtocolError) as err:
            _LOGGER.warning(
                "Reading the meter's scaling properties failed (%s); "
                "using ECHONET defaults",
                err,
            )
        except BaseException:
            self._link = None
            raise
        return link

    def _remember_network(self, network: CachedNetwork) -> None:
        """Keep the joined network so the next connect can skip the scan."""
        if network == self._cached_network:
            return
        self._cached_network = network
        if self._network_cache is not None:
            self._network_cache.store(network)

    async def _async_reset(self) -> None:
        self._arm(commands.NotificationCode.STARTUP_COMPLETED)
        await self._async_write(commands.reset_request())
        await self._async_wait_for(
            commands.NotificationCode.STARTUP_COMPLETED, self._config.startup_timeout
        )

    async def _async_initial_settings(self, channel: int) -> None:
        await self._async_request(
            commands.initial_settings_request(channel),
            commands.CommandCode.SET_INITIAL_SETTINGS,
        )

    async def _async_set_credentials(self) -> None:
        try:
            await self._async_request(
                commands.set_credentials_request(
                    self._config.auth_id, self._config.password
                ),
                commands.CommandCode.SET_CREDENTIALS,
            )
        except commands.CommandFailedError as err:
            if err.result == _CREDENTIALS_ALREADY_SET:
                _LOGGER.debug("Route-B credentials were already stored")
                return
            raise

    async def _async_scan(self) -> commands.ActiveScanResult:
        """Scan for the meter, dwelling longer after every fruitless attempt.

        Real installations regularly need more than one scan: a meter that stays
        silent through a short dwell answers a longer one from the same adapter.
        """
        duration = self._config.scan_duration
        for attempt in range(1, _SCAN_ATTEMPTS + 1):
            try:
                return await self._async_scan_once(duration)
            except MeterNotFoundError:
                if attempt == _SCAN_ATTEMPTS:
                    raise
                duration = min(duration + _SCAN_DURATION_STEP, _MAX_SCAN_DURATION)
                _LOGGER.debug(
                    "The active scan found no meter; retrying with duration %s",
                    duration,
                )
        raise MeterNotFoundError("the active scan found no smart meter")

    async def _async_scan_once(self, duration: int) -> commands.ActiveScanResult:
        """Run one active scan for the configured authentication ID."""
        while not self._scan_results.empty():
            self._scan_results.get_nowait()
        # The adapter answers ACTIVE_SCAN only once it has dwelled on every
        # channel (ROHM's request, result notifications, response order), so
        # both waits carry the dwell budget: the configured timeout would cut
        # a long scan short before its response ever arrives.
        budget = max(
            scan_budget(duration, self._config.channel_mask),
            self._config.scan_timeout,
        )
        _LOGGER.debug(
            "Waiting up to %.1f s for the scan at duration %s to finish",
            budget,
            duration,
        )
        await self._async_request(
            commands.active_scan_request(
                duration=duration,
                channel_mask=self._config.channel_mask,
                auth_id=self._config.auth_id,
            ),
            commands.CommandCode.ACTIVE_SCAN,
            timeout=budget,
        )
        deadline = asyncio.get_running_loop().time() + budget
        scanned: set[int] = set()
        expected = _channels_in_mask(self._config.channel_mask)
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise MeterNotFoundError(
                    "the active scan finished without a smart meter response"
                )
            try:
                result = await asyncio.wait_for(self._scan_results.get(), remaining)
            except TimeoutError:
                raise MeterNotFoundError(
                    "the active scan finished without a smart meter response"
                ) from None
            if result.responded and result.beacons:
                return result
            scanned.add(result.channel)
            if expected and scanned >= expected:
                raise MeterNotFoundError(
                    "the active scan covered every channel without a response"
                )

    async def _async_start_pana(self, mac_address: bytes) -> None:
        self._arm(commands.NotificationCode.PANA_RESULT)
        await self._async_request(
            commands.start_pana_request(), commands.CommandCode.START_PANA
        )
        frame = await self._async_wait_for(
            commands.NotificationCode.PANA_RESULT, self._config.pana_timeout
        )
        result = commands.parse_pana_result_notification(frame)
        if result.result == commands.PanaResultCode.NO_RESPONSE:
            # The meter never answered. That is a radio problem, not a rejected
            # credential, so the session stays retryable.
            raise SessionClosedError("the meter did not answer the PANA exchange")
        if not result.succeeded:
            self._authentication_failed = True
            raise AuthenticationError(
                "the meter rejected the Route-B credentials "
                f"(result 0x{int(result.result):02X})"
            )
        if result.mac_address != mac_address:
            raise SessionError("PANA completed with an unexpected device")

    async def _async_read_profile(self) -> MeterProfile:
        values = await self._async_get_properties(SCALING_PROPERTIES, required=False)
        try:
            values |= await self._async_get_properties(
                IDENTITY_PROPERTIES, required=False
            )
        except (SessionError, ProtocolError) as err:
            _LOGGER.debug("The meter reported no identity properties: %s", err)
        unit = values.get(Epc.CUMULATIVE_UNIT)
        coefficient = values.get(Epc.COEFFICIENT)
        digits = values.get(Epc.CUMULATIVE_DIGITS)
        manufacturer = values.get(Epc.MANUFACTURER_CODE)
        version = values.get(Epc.STANDARD_VERSION)
        serial = values.get(Epc.SERIAL_NUMBER)
        return MeterProfile(
            coefficient=decode_coefficient(coefficient) if coefficient else 1,
            unit=unit_multiplier(unit) if unit else Decimal(1),
            digits=decode_cumulative_digits(digits) if digits else 6,
            manufacturer_code=(
                decode_manufacturer_code(manufacturer) if manufacturer else None
            ),
            standard_version=decode_protocol_version(version) if version else None,
            serial_number=decode_serial_number(serial) if serial else None,
        )

    def _decode_reading(self, values: dict[int, bytes]) -> MeterReading:
        current = decode_instantaneous_current(values[Epc.INSTANTANEOUS_CURRENT])
        return MeterReading(
            instantaneous_power=decode_instantaneous_power(
                values[Epc.INSTANTANEOUS_POWER]
            ),
            current_r_phase=current.r_phase,
            current_t_phase=current.t_phase,
            cumulative_forward_energy=decode_cumulative_energy(
                values[Epc.CUMULATIVE_FORWARD_ENERGY], self._profile
            ),
            cumulative_reverse_energy=decode_cumulative_energy(
                values[Epc.CUMULATIVE_REVERSE_ENERGY], self._profile
            ),
        )

    async def _async_get_properties(
        self, epcs: Sequence[Epc], *, required: bool = True
    ) -> dict[int, bytes]:
        """Get ``epcs`` from the meter, retrying a lost transaction."""
        last_error: Exception | None = None
        for attempt in range(1, self._config.echonet_attempts + 1):
            if attempt > 1:
                self._stats.echonet_retries += 1
            try:
                values = await self._async_echonet_get(epcs)
            except (SessionTimeoutError, TransmissionError, EchonetFrameError) as err:
                last_error = err
                continue
            missing = [epc for epc in epcs if not values.get(epc)]
            if missing and required:
                last_error = SessionError(
                    "the meter did not return "
                    + ", ".join(f"0x{epc:02X}" for epc in missing)
                )
                continue
            return values
        if last_error is None:
            raise SessionError("no properties were requested")
        if isinstance(last_error, SessionError):
            raise last_error
        raise SessionError(str(last_error)) from last_error

    async def _async_echonet_get(self, epcs: Sequence[Epc]) -> dict[int, bytes]:
        link = self._link
        if link is None:
            raise SessionClosedError("no PANA session is established")
        self._transaction_id = (self._transaction_id + 1) % (_MAX_TRANSACTION_ID + 1)
        transaction_id = self._transaction_id
        payload = encode_get(list(epcs), transaction_id=transaction_id)
        result = commands.parse_transmit_data_response(
            await self._async_request(
                commands.transmit_data_request(link.address, payload),
                commands.CommandCode.TRANSMIT_DATA,
            )
        )
        if not result.transmission_succeeded and not result.queued:
            message = (
                f"the adapter could not transmit the request "
                f"(result 0x{result.transmission_result:X})"
            )
            if result.retryable:
                raise TransmissionError(message)
            raise SessionError(message)
        return await self._async_await_get_response(transaction_id)

    async def _async_await_get_response(self, transaction_id: int) -> dict[int, bytes]:
        """Wait for the response to ``transaction_id``, skipping other traffic."""
        deadline = asyncio.get_running_loop().time() + self._config.echonet_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self._stats.timeouts += 1
                raise SessionTimeoutError("the meter did not answer the Get request")
            try:
                datagram = await asyncio.wait_for(self._datagrams.get(), remaining)
            except TimeoutError:
                self._stats.timeouts += 1
                raise SessionTimeoutError(
                    "the meter did not answer the Get request"
                ) from None
            try:
                frame = EchonetLiteFrame.decode(datagram.payload)
            except EchonetFrameError as err:
                _LOGGER.debug("Ignoring an undecodable datagram: %s", err)
                continue
            if (
                frame.transaction_id != transaction_id
                or frame.source_object != LOW_VOLTAGE_METER_OBJECT
            ):
                self._stats.unsolicited_datagrams += 1
                continue
            if frame.esv == Esv.GET_SNA:
                # A Get_SNA still carries the properties the meter could read;
                # only the refused ones come back empty. Keeping the successful
                # ones means one unsupported optional property cannot cost us
                # the scaling factors that share the request.
                values = frame.property_map()
                refused = [epc for epc, edt in values.items() if not edt]
                _LOGGER.debug(
                    "The meter refused %s",
                    ", ".join(f"0x{epc:02X}" for epc in refused),
                )
                return values
            if not frame.is_get_response:
                self._stats.unsolicited_datagrams += 1
                continue
            return frame.property_map()

    async def _async_request(
        self,
        request: bytes,
        command: commands.CommandCode,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - deadlines come from the config
    ) -> Frame:
        """Send ``request`` and return the adapter's matching response."""
        expected = response_code(command)
        loop = asyncio.get_running_loop()
        async with self._transaction_lock:
            future: asyncio.Future[Frame] = loop.create_future()
            self._pending[expected] = future
            try:
                await self._async_write(request)
                self._stats.requests += 1
                frame = await asyncio.wait_for(
                    future, timeout or self._config.command_timeout
                )
            except TimeoutError:
                self._stats.timeouts += 1
                raise SessionTimeoutError(
                    f"the adapter did not answer command 0x{command:04X}"
                ) from None
            finally:
                self._pending.pop(expected, None)
                if future.done() and not future.cancelled():
                    # Losing the link completes the future through
                    # _fail_waiters, which can happen after we stopped waiting
                    # for it. Retrieve it so asyncio does not report the
                    # exception as never retrieved.
                    future.exception()
        commands.raise_for_result(frame)
        return frame

    def _arm(self, code: int) -> None:
        """Discard stale ``code`` notifications before triggering a new one.

        The adapter often sends a notification in the same UART burst as the
        response that precedes it, so waiting has to be queue-based: arming
        happens before the triggering request is written.
        """
        queue = self._notifications.setdefault(code, asyncio.Queue())
        while not queue.empty():
            queue.get_nowait()

    async def _async_wait_for(
        self,
        code: int,
        timeout: float,  # noqa: ASYNC109 - deadlines come from the config
    ) -> Frame:
        """Wait for notification ``code``, or for the link to disappear."""
        queue = self._notifications.setdefault(code, asyncio.Queue())
        getter = asyncio.ensure_future(queue.get())
        lost = asyncio.ensure_future(self._link_lost.wait())
        try:
            done, _ = await asyncio.wait(
                (getter, lost),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if getter in done:
                return getter.result()
            if lost in done:
                raise SessionClosedError("the session was closed while waiting")
            self._stats.timeouts += 1
            raise SessionTimeoutError(f"the adapter sent no 0x{code:04X} notification")
        finally:
            for task in (getter, lost):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _async_write(self, request: bytes) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._transport.write, request)
        except TransportError as err:
            self._handle_link_loss(err)
            raise SessionClosedError(str(err)) from err

    async def _async_read_loop(self) -> None:
        """Feed the reassembler from the transport until the session closes."""
        loop = asyncio.get_running_loop()
        try:
            while True:
                chunk = await loop.run_in_executor(
                    None, self._transport.read, READ_CHUNK
                )
                if not chunk:
                    continue
                for frame in self._reassembler.feed(chunk):
                    self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except TransportError as err:
            if not self._closing:
                _LOGGER.warning("The adapter's serial link failed: %s", err)
            self._handle_link_loss(err)
        finally:
            self._stats.frames = self._reassembler.stats

    def _dispatch(self, frame: Frame) -> None:
        """Route one decoded frame to whoever is waiting for it."""
        if frame.is_response:
            future = self._pending.pop(frame.command_code, None)
            if future is not None and not future.done():
                future.set_result(frame)
            else:
                _LOGGER.debug(
                    "Discarding a late response for 0x%04X", frame.command_code
                )
            return
        if not frame.is_event:
            _LOGGER.debug("Ignoring frame 0x%04X from the adapter", frame.command_code)
            return
        self._stats.notifications += 1
        self._handle_notification(frame)
        queue = self._notifications.get(frame.command_code)
        if queue is not None:
            queue.put_nowait(frame)

    def _handle_notification(self, frame: Frame) -> None:
        try:
            if frame.command_code == commands.NotificationCode.DATA_RECEPTION:
                self._datagrams.put_nowait(
                    commands.parse_data_reception_notification(frame)
                )
            elif frame.command_code == commands.NotificationCode.ACTIVE_SCAN_RESULT:
                self._scan_results.put_nowait(
                    commands.parse_active_scan_notification(frame)
                )
            elif frame.command_code == commands.NotificationCode.CONNECTION_STATUS:
                status = commands.parse_connection_status_notification(frame)
                if status.disconnected:
                    self._handle_link_loss(
                        SessionClosedError("the meter left the connected state")
                    )
            elif (
                frame.command_code == commands.NotificationCode.PACKET_RECEPTION_FAILURE
            ):
                failure = commands.parse_reception_failure_notification(frame)
                _LOGGER.debug("The adapter dropped a packet: %s", failure.reason)
        except ProtocolError as err:
            _LOGGER.debug("Ignoring an undecodable notification: %s", err)

    def _handle_link_loss(self, error: Exception) -> None:
        """Mark the session as disconnected and wake every waiter."""
        was_connected = self._link is not None
        self._link = None
        self._fail_waiters(error)
        if was_connected and self._on_disconnect is not None:
            self._on_disconnect()

    def _fail_waiters(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        self._link_lost.set()


#: Response result meaning the adapter already holds these credentials.
_CREDENTIALS_ALREADY_SET: Final = 0x58
#: Response result meaning the MAC association with the meter failed; a later
#: attempt from a reset module regularly succeeds (specification Table 34).
_MAC_CONNECTION_FAILED: Final = 0x0E
#: How many times :meth:`J11Session.async_ensure_connected` rebuilds the link
#: before giving up and letting the coordinator report the entry unavailable.
_RECONNECT_ATTEMPTS: Final = 3


def _channels_in_mask(mask: int) -> set[int]:
    """Return the channel numbers selected by ``mask``.

    Bit *n* of the scan command's channel mask selects channel *n*
    (specification §3.2.3.4), so the adapter reports one scan notification per
    set bit and the scan is over once they have all arrived.
    """
    return {bit for bit in range(32) if mask >> bit & 1}
