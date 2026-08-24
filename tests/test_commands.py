"""Tests for J11 request encoders and response/notification decoders."""

from __future__ import annotations

from ipaddress import IPv6Address

import pytest

from custom_components.broute_j11.protocol.codec import (
    Frame,
    FrameFormatError,
    decode_frame,
)
from custom_components.broute_j11.protocol.commands import (
    AccessPointStatus,
    CommandCode,
    CommandFailedError,
    CredentialFormatError,
    NotificationCode,
    PanaResultCode,
    ReceptionFailureReason,
    active_scan_request,
    initial_settings_request,
    link_local_address,
    open_udp_port_request,
    parse_active_scan_notification,
    parse_connection_status_notification,
    parse_data_reception_notification,
    parse_pana_result_notification,
    parse_reception_failure_notification,
    parse_response_result,
    parse_route_b_start_response,
    parse_transmit_data_response,
    parse_version_response,
    raise_for_result,
    reset_request,
    set_credentials_request,
    start_pana_request,
    start_route_b_request,
    terminate_pana_request,
    transmit_data_request,
    validate_auth_id,
    validate_password,
    version_request,
)

from .fixtures import frames

METER_MAC = bytes.fromhex("0050C2FFFEDC2822")
METER_ADDRESS = IPv6Address("fe80::250:c2ff:fedc:2822")
SYNTHETIC_AUTH_ID = "0000000000000000000000000000ABCD"
SYNTHETIC_PASSWORD = "SYNTHETICPW1"


def test_reset_request_matches_documented_frame() -> None:
    assert reset_request() == frames.RESET_REQUEST


def test_version_request_has_no_data() -> None:
    frame = decode_frame(version_request())
    assert frame.command_code == CommandCode.GET_VERSION
    assert frame.data == b""


def test_initial_settings_request_matches_documented_frame() -> None:
    assert initial_settings_request(4) == frames.INITIAL_SETTINGS_REQUEST_CHANNEL_4
    assert initial_settings_request(12) == frames.INITIAL_SETTINGS_REQUEST_CHANNEL_12


@pytest.mark.parametrize("channel", [3, 18])
def test_initial_settings_request_rejects_channel_outside_wisun_band(
    channel: int,
) -> None:
    with pytest.raises(ValueError, match="channel"):
        initial_settings_request(channel)


def test_credentials_request_matches_documented_frame() -> None:
    encoded = set_credentials_request(
        frames.DOCUMENTED_AUTH_ID, frames.DOCUMENTED_PASSWORD
    )
    assert encoded == frames.CREDENTIALS_REQUEST


def test_credentials_request_uppercases_hexadecimal_ids() -> None:
    assert (
        set_credentials_request(
            frames.DOCUMENTED_AUTH_ID.lower(), frames.DOCUMENTED_PASSWORD
        )
        == frames.CREDENTIALS_REQUEST
    )


@pytest.mark.parametrize(
    "auth_id",
    [
        "",
        SYNTHETIC_AUTH_ID[:-1],
        SYNTHETIC_AUTH_ID + "E",
        SYNTHETIC_AUTH_ID[:-1] + "G",
        SYNTHETIC_AUTH_ID[:-2] + " D",
    ],
)
def test_validate_auth_id_rejects_malformed_values(auth_id: str) -> None:
    with pytest.raises(CredentialFormatError) as excinfo:
        validate_auth_id(auth_id)
    assert not auth_id or auth_id not in str(excinfo.value)


def test_validate_auth_id_normalises_case() -> None:
    assert validate_auth_id(SYNTHETIC_AUTH_ID.lower()) == SYNTHETIC_AUTH_ID


@pytest.mark.parametrize(
    "password",
    ["", "SHORTPW", "TOOLONGPASSWORD", "SYNTHETIC-PW", "SYNTHETIC PW"],
)
def test_validate_password_rejects_malformed_values(password: str) -> None:
    with pytest.raises(CredentialFormatError) as excinfo:
        validate_password(password)
    assert not password or password not in str(excinfo.value)


def test_validate_password_keeps_case() -> None:
    assert validate_password(SYNTHETIC_PASSWORD) == SYNTHETIC_PASSWORD


def test_credential_errors_never_leak_the_value() -> None:
    with pytest.raises(CredentialFormatError) as excinfo:
        set_credentials_request(SYNTHETIC_AUTH_ID, "bad")
    message = str(excinfo.value)
    assert "bad" not in message
    assert SYNTHETIC_AUTH_ID not in message


def test_active_scan_request_matches_documented_frame() -> None:
    encoded = active_scan_request(
        duration=6,
        channel_mask=0x0003FFF0,
        pairing_id=frames.DOCUMENTED_AUTH_ID[-8:].encode("ascii"),
    )
    assert encoded == frames.ACTIVE_SCAN_REQUEST


def test_active_scan_request_derives_pairing_id_from_auth_id() -> None:
    assert (
        active_scan_request(
            duration=6,
            channel_mask=0x0003FFF0,
            auth_id=frames.DOCUMENTED_AUTH_ID,
        )
        == frames.ACTIVE_SCAN_REQUEST
    )


def test_active_scan_request_without_pairing_id_clears_the_id_flag() -> None:
    frame = decode_frame(active_scan_request(duration=6, channel_mask=0x0003FFF0))
    assert frame.data == b"\x06\x00\x03\xff\xf0\x00" + bytes(8)


@pytest.mark.parametrize("duration", [0, 15])
def test_active_scan_request_rejects_out_of_range_duration(duration: int) -> None:
    with pytest.raises(ValueError, match="duration"):
        active_scan_request(duration=duration, channel_mask=0x0003FFF0)


def test_active_scan_request_rejects_out_of_range_channel_mask() -> None:
    with pytest.raises(ValueError, match="channel mask"):
        active_scan_request(duration=6, channel_mask=0x0004_0000)


def test_active_scan_request_rejects_short_pairing_id() -> None:
    with pytest.raises(ValueError, match="pairing id"):
        active_scan_request(duration=6, channel_mask=0x10, pairing_id=b"SHORT")


def test_route_b_and_pana_requests_match_documented_frames() -> None:
    assert start_route_b_request() == frames.ROUTE_B_START_REQUEST
    assert start_pana_request() == frames.PANA_START_REQUEST
    assert decode_frame(terminate_pana_request()).command_code == (
        CommandCode.TERMINATE_PANA
    )


def test_open_udp_port_request_matches_documented_frame() -> None:
    assert open_udp_port_request(3610) == frames.UDP_OPEN_REQUEST


@pytest.mark.parametrize("port", [0, 0x1_0000])
def test_open_udp_port_request_rejects_invalid_port(port: int) -> None:
    with pytest.raises(ValueError, match="port"):
        open_udp_port_request(port)


def test_link_local_address_inverts_the_universal_local_bit() -> None:
    assert link_local_address(METER_MAC) == METER_ADDRESS


def test_link_local_address_rejects_short_mac() -> None:
    with pytest.raises(ValueError, match="MAC"):
        link_local_address(b"\x00\x01")


def test_transmit_data_request_matches_documented_frame() -> None:
    payload = frames.TRANSMIT_DATA_REQUEST[-16:]
    assert transmit_data_request(METER_ADDRESS, payload) == frames.TRANSMIT_DATA_REQUEST


def test_transmit_data_request_rejects_empty_payload() -> None:
    with pytest.raises(ValueError, match="payload"):
        transmit_data_request(METER_ADDRESS, b"")


def test_transmit_data_request_rejects_oversized_payload() -> None:
    with pytest.raises(ValueError, match="payload"):
        transmit_data_request(METER_ADDRESS, b"\x00" * 1233)


def test_parse_response_result_reads_the_result_byte() -> None:
    assert parse_response_result(decode_frame(frames.UDP_OPEN_RESPONSE)) == 0x01


def test_parse_response_result_rejects_empty_data() -> None:
    with pytest.raises(FrameFormatError):
        parse_response_result(Frame(command_code=0x2005))


def test_raise_for_result_accepts_success() -> None:
    raise_for_result(decode_frame(frames.UDP_OPEN_RESPONSE))


def test_raise_for_result_describes_known_failures() -> None:
    frame = Frame(command_code=0x2005, data=b"\x0a")
    with pytest.raises(CommandFailedError) as excinfo:
        raise_for_result(frame)
    assert excinfo.value.result == 0x0A
    assert "Already open port number" in str(excinfo.value)


def test_raise_for_result_describes_unknown_failures() -> None:
    with pytest.raises(CommandFailedError, match="0x77"):
        raise_for_result(Frame(command_code=0x2005, data=b"\x77"))


def test_parse_version_response() -> None:
    frame = Frame(
        command_code=0x206B,
        data=bytes.fromhex("01 0400 01 02 0000000A"),
    )
    version = parse_version_response(frame)
    assert version.firmware_id == 0x0400
    assert version.major == 1
    assert version.minor == 2
    assert version.revision == 10
    assert str(version) == "1.2.10"


def test_parse_version_response_rejects_truncated_data() -> None:
    with pytest.raises(FrameFormatError):
        parse_version_response(Frame(command_code=0x206B, data=b"\x01\x04"))


def test_parse_route_b_start_response() -> None:
    result = parse_route_b_start_response(decode_frame(frames.ROUTE_B_START_RESPONSE))
    assert result.channel == 12
    assert result.pan_id == 0xBCDE
    assert result.mac_address == METER_MAC
    assert result.rssi == -34
    assert result.address == METER_ADDRESS


def test_parse_route_b_start_response_reports_failure_without_details() -> None:
    with pytest.raises(CommandFailedError):
        parse_route_b_start_response(Frame(command_code=0x2053, data=b"\x0e"))


def test_parse_active_scan_notification_without_beacon() -> None:
    result = parse_active_scan_notification(
        decode_frame(frames.ACTIVE_SCAN_NOTIFICATION_EMPTY)
    )
    assert not result.responded
    assert result.channel == 4
    assert result.beacons == ()


def test_parse_active_scan_notification_with_beacon() -> None:
    result = parse_active_scan_notification(
        decode_frame(frames.ACTIVE_SCAN_NOTIFICATION_RESPONSE)
    )
    assert result.responded
    assert result.channel == 12
    assert [
        (beacon.mac_address, beacon.pan_id, beacon.rssi) for beacon in result.beacons
    ] == [(METER_MAC, 0xBCDE, -34)]


def test_parse_active_scan_notification_rejects_truncated_beacon() -> None:
    truncated = Frame(
        command_code=NotificationCode.ACTIVE_SCAN_RESULT, data=b"\x00\x0c\x01\x00\x50"
    )
    with pytest.raises(FrameFormatError):
        parse_active_scan_notification(truncated)


def test_parse_data_reception_notification() -> None:
    datagram = parse_data_reception_notification(
        decode_frame(frames.DATA_RECEPTION_NOTIFICATION)
    )
    assert datagram.source == METER_ADDRESS
    assert datagram.source_port == 3610
    assert datagram.destination_port == 3610
    assert datagram.source_pan_id == 0x22A9
    assert not datagram.multicast
    assert datagram.encrypted
    assert datagram.rssi == -53
    assert len(datagram.payload) == 38


def test_parse_data_reception_notification_rejects_payload_length_mismatch() -> None:
    raw = bytearray(frames.DATA_RECEPTION_NOTIFICATION)
    data = bytes(raw[12:])
    corrupt = data[:25] + (0x0100).to_bytes(2, "big") + data[27:]
    with pytest.raises(FrameFormatError):
        parse_data_reception_notification(
            Frame(command_code=NotificationCode.DATA_RECEPTION, data=corrupt)
        )


def test_parse_pana_result_notification() -> None:
    result = parse_pana_result_notification(
        decode_frame(frames.PANA_RESULT_NOTIFICATION)
    )
    assert result.result is PanaResultCode.SUCCEEDED
    assert result.mac_address == METER_MAC
    assert result.succeeded


def test_parse_pana_result_notification_reports_failure() -> None:
    result = parse_pana_result_notification(
        Frame(command_code=NotificationCode.PANA_RESULT, data=b"\x02" + METER_MAC)
    )
    assert result.result is PanaResultCode.FAILED
    assert not result.succeeded


def test_parse_pana_result_notification_keeps_unknown_codes() -> None:
    result = parse_pana_result_notification(
        Frame(command_code=NotificationCode.PANA_RESULT, data=b"\x09" + METER_MAC)
    )
    assert result.result == 0x09
    assert not result.succeeded


def test_parse_connection_status_notification() -> None:
    change = parse_connection_status_notification(
        Frame(
            command_code=NotificationCode.CONNECTION_STATUS,
            data=b"\x04" + METER_MAC + b"\xde",
        )
    )
    assert change.status is AccessPointStatus.PANA_DISCONNECTED
    assert change.mac_address == METER_MAC
    assert change.rssi == -34
    assert change.disconnected


def test_parse_connection_status_notification_flags_connected_states() -> None:
    change = parse_connection_status_notification(
        Frame(
            command_code=NotificationCode.CONNECTION_STATUS,
            data=b"\x02" + METER_MAC + b"\xde",
        )
    )
    assert change.status is AccessPointStatus.PANA_CONNECTED
    assert not change.disconnected


def test_parse_reception_failure_notification() -> None:
    failure = parse_reception_failure_notification(
        Frame(
            command_code=NotificationCode.PACKET_RECEPTION_FAILURE,
            data=b"\x01"
            + METER_ADDRESS.packed
            + b"\x2a"
            + b"\x01"
            + b"\x00\x00"
            + b"\x10\x81",
        )
    )
    assert failure.reason is ReceptionFailureReason.DECODING
    assert failure.source == METER_ADDRESS
    assert failure.sequence_number == 0x2A
    assert not failure.fragmented


def test_parse_transmit_data_response() -> None:
    result = parse_transmit_data_response(decode_frame(frames.TRANSMIT_DATA_RESPONSE))
    assert result.queued is False
    assert result.transmission_succeeded


def test_parse_transmit_data_response_detects_transmission_failure() -> None:
    frame = Frame(command_code=0x2008, data=b"\x01\x03\x10\x81")
    result = parse_transmit_data_response(frame)
    assert not result.transmission_succeeded


def test_parse_transmit_data_response_raises_on_command_failure() -> None:
    with pytest.raises(CommandFailedError):
        parse_transmit_data_response(Frame(command_code=0x2008, data=b"\x04"))
