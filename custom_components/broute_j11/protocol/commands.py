"""Requests, responses and notifications of the BP35C0-J11 UART protocol.

Field layouts and value tables come from ROHM's "BP35C0-J11 UART IF
specification" (No. 63TR008E): §3.2 for the common commands, §3.4 for the
Route-B commands, Table 20 for the initial settings, Table 21 for the channel
numbers and Table 34 for the response results.

Only the commands a B-route smart-meter session needs are implemented. Nothing
here formats credentials or meter identifiers into exception messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from ipaddress import IPv6Address
import string
from typing import Final

from .codec import Frame, FrameFormatError, ProtocolError, encode_request

AUTH_ID_LENGTH: Final = 32
PASSWORD_LENGTH: Final = 12
PAIRING_ID_LENGTH: Final = 8
MAC_ADDRESS_LENGTH: Final = 8

MIN_CHANNEL: Final = 0x04
MAX_CHANNEL: Final = 0x11
MIN_SCAN_DURATION: Final = 0x01
MAX_SCAN_DURATION: Final = 0x0E
ALL_CHANNELS_MASK: Final = 0x0003FFF0

ECHONET_PORT: Final = 3610
MAX_UDP_PAYLOAD: Final = 1232
MIN_PORT: Final = 1
MAX_PORT: Final = 0xFFFF

RESULT_SUCCEEDED: Final = 0x01
#: Transmission result of a Transmit Data response meaning the frame went out.
TRANSMISSION_SUCCEEDED: Final = 0x0
#: Transmission result meaning the destination never acknowledged the frame.
TRANSMISSION_NO_ACK: Final = 0x5
#: Transmission results a later attempt can still succeed at.
RETRYABLE_TRANSMISSION_RESULTS: Final = frozenset({TRANSMISSION_NO_ACK})

_HEX_DIGITS: Final = frozenset(string.hexdigits)
_ALPHANUMERIC: Final = frozenset(string.ascii_letters + string.digits)
_ENCRYPTED: Final = 0x02
_RSSI_OFFSET: Final = -256


class CommandCode(IntEnum):
    """Request command codes used by this integration."""

    OPEN_UDP_PORT = 0x0005
    TRANSMIT_DATA = 0x0008
    ACTIVE_SCAN = 0x0051
    START_ROUTE_B = 0x0053
    SET_CREDENTIALS = 0x0054
    START_PANA = 0x0056
    TERMINATE_PANA = 0x0057
    SET_INITIAL_SETTINGS = 0x005F
    GET_VERSION = 0x006B
    RESET = 0x00D9


class NotificationCode(IntEnum):
    """Notification command codes the adapter sends spontaneously."""

    ACTIVE_SCAN_RESULT = 0x4051
    DATA_RECEPTION = 0x6018
    STARTUP_COMPLETED = 0x6019
    CONNECTION_STATUS = 0x601A
    PANA_RESULT = 0x6028
    PACKET_RECEPTION_FAILURE = 0x6038


class OperationMode(IntEnum):
    """Table 20 operation modes."""

    HAN_PAN_COORDINATOR = 0x01
    HAN_COORDINATOR = 0x02
    HAN_END_DEVICE = 0x03
    DUAL = 0x05


class TransmissionPower(IntEnum):
    """Table 20 transmission powers."""

    MW_20 = 0x00
    MW_10 = 0x01
    MW_1 = 0x02


class PanaResultCode(IntEnum):
    """Results reported by Notify PANA Authentication Result (§3.2.5.4)."""

    SUCCEEDED = 0x01
    FAILED = 0x02
    NO_RESPONSE = 0x03


class AccessPointStatus(IntEnum):
    """States reported by Notify Connection Status Change (§3.2.5.3)."""

    MAC_CONNECTED = 0x01
    PANA_CONNECTED = 0x02
    MAC_DISCONNECTED = 0x03
    PANA_DISCONNECTED = 0x04


class ReceptionFailureReason(IntEnum):
    """Reasons reported by Notify Packet Reception Failure (§3.2.5.5)."""

    DECODING = 0x01
    MAC = 0x02
    SIXLOWPAN = 0x20
    IP = 0x30
    UDP = 0x40


#: Table 34, "List of response results to Request command".
RESPONSE_RESULTS: Final[dict[int, str]] = {
    0x01: "Command succeeded",
    0x02: "The specified address does not exist in the device list",
    0x03: "Invalid command code",
    0x04: "Invalid parameter value",
    0x06: "Transmission error due to invalid address",
    0x0A: "Port opening error: Already open port number",
    0x0B: "Port closing error: Unopened port number",
    0x0E: "MAC connection failed",
    0x0F: "Unexecutable due to HAN in the operating status or mismatched mode",
    0x10: "Unexecutable due to Route B or HAN in the not-yet-started status",
    0x11: "The specified parameter length is outside the permitted range",
    0x12: "Maximum number of opened ports exceeded",
    0x13: "Command reception error: data reception time (1 second) expired",
    0x14: "Unexecutable operation mode",
    0x20: "The specified HAN acceptance connection mode is already current",
    0x21: "Switch HAN Acceptance Connection Mode is unexecutable in this mode",
    0x33: "Unexecutable due to HAN in the authentication status",
    0x34: "Unexecutable due to Route B in the operating status",
    0x35: "Unexecutable due to Route B in the authentication status",
    0x37: "Unexecutable due to the whole block in the not-yet-started status",
    0x3C: "Transmit To Ping requested again before its notification",
    0x3D: "Another request is still being processed",
    0x3E: "The PAN ID equals the Route-B PAN ID or 0xFFFF",
    0x3F: "Transition to deep sleep mode failed",
    0x46: "Poll request failed",
    0x51: "PANA execution error: inadequate setting or information ungenerated",
    0x52: "PANA execution error: PANA sequence in operation",
    0x53: "PANA execution error: no information for the specified address",
    0x58: "PANA execution error: authentication information has been set",
    0x59: "PANA execution error: maximum set number exceeded",
    0x61: "Invalid OTA Client status",
    0xF0: "Command reception error: header checksum error",
    0xF1: "Command reception error: data checksum error",
    0xF2: "Command reception error: message length shorter than the header claims",
    0xF3: "Command reception error: message length exceeds the maximum",
}


class CredentialFormatError(ProtocolError):
    """A Route-B credential does not have the shape the adapter requires.

    The offending value is never included in the message: these errors surface
    in the config flow and in logs.
    """


class CommandFailedError(ProtocolError):
    """The adapter rejected a request or reported a failure result."""

    def __init__(self, command_code: int, result: int) -> None:
        """Describe ``result`` using Table 34 where possible."""
        description = RESPONSE_RESULTS.get(result, f"unknown result 0x{result:02X}")
        super().__init__(
            f"command 0x{command_code:04X} failed with result "
            f"0x{result:02X}: {description}"
        )
        self.command_code = command_code
        self.result = result


def validate_auth_id(auth_id: str) -> str:
    """Return ``auth_id`` upper-cased, or raise if it is not 32 hex characters."""
    stripped = auth_id.strip()
    if len(stripped) != AUTH_ID_LENGTH or not set(stripped) <= _HEX_DIGITS:
        raise CredentialFormatError(
            f"Route-B authentication ID must be {AUTH_ID_LENGTH} hexadecimal "
            f"characters, got {len(stripped)} characters"
        )
    return stripped.upper()


def validate_password(password: str) -> str:
    """Return ``password`` unchanged, or raise if it is not 12 alphanumerics."""
    if len(password) != PASSWORD_LENGTH or not set(password) <= _ALPHANUMERIC:
        raise CredentialFormatError(
            f"Route-B password must be {PASSWORD_LENGTH} ASCII alphanumeric "
            f"characters, got {len(password)} characters"
        )
    return password


def link_local_address(mac_address: bytes) -> IPv6Address:
    """Return the link-local address of ``mac_address``.

    The module reports MAC addresses with the universal/local bit cleared, so
    the address it accepts has the second-lowest bit of the first byte set.
    """
    if len(mac_address) != MAC_ADDRESS_LENGTH:
        raise ValueError(
            f"MAC address must be {MAC_ADDRESS_LENGTH} bytes, got {len(mac_address)}"
        )
    interface_id = bytes([mac_address[0] ^ 0x02, *mac_address[1:]])
    return IPv6Address(b"\xfe\x80" + bytes(6) + interface_id)


def version_request() -> bytes:
    """Encode Get Version Information (§3.2.4.1)."""
    return encode_request(CommandCode.GET_VERSION)


def reset_request() -> bytes:
    """Encode Reset Hardware (§3.2.4.2), which answers with a startup notification."""
    return encode_request(CommandCode.RESET)


def initial_settings_request(
    channel: int,
    *,
    operation_mode: OperationMode = OperationMode.DUAL,
    han_sleep: bool = False,
    transmission_power: TransmissionPower = TransmissionPower.MW_20,
) -> bytes:
    """Encode Set Initial Settings (§3.2.2.1) for ``channel``."""
    if not MIN_CHANNEL <= channel <= MAX_CHANNEL:
        raise ValueError(
            f"channel must be between {MIN_CHANNEL} and {MAX_CHANNEL}, got {channel}"
        )
    data = bytes([operation_mode, int(han_sleep), channel, transmission_power])
    return encode_request(CommandCode.SET_INITIAL_SETTINGS, data)


def set_credentials_request(auth_id: str, password: str) -> bytes:
    """Encode Set Route-B PANA Authentication Information (§3.4.2.1)."""
    data = validate_auth_id(auth_id).encode("ascii") + validate_password(
        password
    ).encode("ascii")
    return encode_request(CommandCode.SET_CREDENTIALS, data)


def pairing_id_from_auth_id(auth_id: str) -> bytes:
    """Return the pairing ID a smart-meter scan needs: the ID's last 8 characters."""
    return validate_auth_id(auth_id)[-PAIRING_ID_LENGTH:].encode("ascii")


def active_scan_request(
    *,
    duration: int,
    channel_mask: int = ALL_CHANNELS_MASK,
    pairing_id: bytes | None = None,
    auth_id: str | None = None,
) -> bytes:
    """Encode Execute Active Scan (§3.2.3.4).

    ``duration`` selects a per-channel dwell time of 9.64 ms * 2 ** duration.
    Either ``pairing_id`` or ``auth_id`` restricts the scan to one meter.
    """
    if not MIN_SCAN_DURATION <= duration <= MAX_SCAN_DURATION:
        raise ValueError(
            f"scan duration must be between {MIN_SCAN_DURATION} and "
            f"{MAX_SCAN_DURATION}, got {duration}"
        )
    if not 0 <= channel_mask <= ALL_CHANNELS_MASK:
        raise ValueError(f"channel mask out of range: 0x{channel_mask:08X}")
    if auth_id is not None:
        pairing_id = pairing_id_from_auth_id(auth_id)
    if pairing_id is not None and len(pairing_id) != PAIRING_ID_LENGTH:
        raise ValueError(
            f"pairing id must be {PAIRING_ID_LENGTH} bytes, got {len(pairing_id)}"
        )
    data = (
        bytes([duration])
        + channel_mask.to_bytes(4, "big")
        + bytes([0x00 if pairing_id is None else 0x01])
        + (pairing_id or bytes(PAIRING_ID_LENGTH))
    )
    return encode_request(CommandCode.ACTIVE_SCAN, data)


def start_route_b_request() -> bytes:
    """Encode Initiate Route-B Operation (§3.4.3.1)."""
    return encode_request(CommandCode.START_ROUTE_B)


def start_pana_request() -> bytes:
    """Encode Initiate Route-B PANA (§3.4.3.2)."""
    return encode_request(CommandCode.START_PANA)


def terminate_pana_request() -> bytes:
    """Encode Terminate Route-B PANA (§3.4.3.3)."""
    return encode_request(CommandCode.TERMINATE_PANA)


def open_udp_port_request(port: int = ECHONET_PORT) -> bytes:
    """Encode Open UDP Port (§3.2.3.1)."""
    if not MIN_PORT <= port <= MAX_PORT:
        raise ValueError(
            f"UDP port must be between {MIN_PORT} and {MAX_PORT}, got {port}"
        )
    return encode_request(CommandCode.OPEN_UDP_PORT, port.to_bytes(2, "big"))


def transmit_data_request(
    destination: IPv6Address,
    payload: bytes,
    *,
    source_port: int = ECHONET_PORT,
    destination_port: int = ECHONET_PORT,
) -> bytes:
    """Encode Transmit Data (§3.2.3.3)."""
    if not 1 <= len(payload) <= MAX_UDP_PAYLOAD:
        raise ValueError(
            f"payload must be between 1 and {MAX_UDP_PAYLOAD} bytes, got {len(payload)}"
        )
    data = (
        destination.packed
        + source_port.to_bytes(2, "big")
        + destination_port.to_bytes(2, "big")
        + len(payload).to_bytes(2, "big")
        + payload
    )
    return encode_request(CommandCode.TRANSMIT_DATA, data)


def _require(frame: Frame, size: int, description: str) -> bytes:
    if len(frame.data) < size:
        raise FrameFormatError(
            f"command 0x{frame.command_code:04X} carries {len(frame.data)} bytes, "
            f"too short for {description} ({size} bytes)"
        )
    return frame.data


def _rssi(raw: int) -> int:
    """Convert a reported RSSI byte to dBm."""
    return raw + _RSSI_OFFSET


def parse_response_result(frame: Frame) -> int:
    """Return the response result byte of ``frame``."""
    return _require(frame, 1, "a response result")[0]


def raise_for_result(frame: Frame) -> int:
    """Return the response result, raising :class:`CommandFailedError` on failure."""
    result = parse_response_result(frame)
    if result != RESULT_SUCCEEDED:
        raise CommandFailedError(frame.command_code, result)
    return result


@dataclass(frozen=True, slots=True)
class VersionInfo:
    """Firmware identity reported by Get Version Information (§3.2.4.1)."""

    firmware_id: int
    major: int
    minor: int
    revision: int

    def __str__(self) -> str:
        """Return a human-readable firmware version."""
        return f"{self.major}.{self.minor}.{self.revision}"


def parse_version_response(frame: Frame) -> VersionInfo:
    """Decode a Get Version Information response."""
    raise_for_result(frame)
    data = _require(frame, 9, "version information")
    return VersionInfo(
        firmware_id=int.from_bytes(data[1:3], "big"),
        major=data[3],
        minor=data[4],
        revision=int.from_bytes(data[5:9], "big"),
    )


@dataclass(frozen=True, slots=True)
class RouteBStartResult:
    """Connection details reported by Initiate Route-B Operation (§3.4.3.1)."""

    channel: int
    pan_id: int
    mac_address: bytes
    rssi: int

    @property
    def address(self) -> IPv6Address:
        """Link-local address of the connected smart meter."""
        return link_local_address(self.mac_address)


def parse_route_b_start_response(frame: Frame) -> RouteBStartResult:
    """Decode an Initiate Route-B Operation response.

    A failed connection omits every parameter after the result, so the failure
    is raised rather than reported as an incomplete result.
    """
    raise_for_result(frame)
    data = _require(frame, 13, "Route-B connection details")
    return RouteBStartResult(
        channel=data[1],
        pan_id=int.from_bytes(data[2:4], "big"),
        mac_address=bytes(data[4:12]),
        rssi=_rssi(data[12]),
    )


@dataclass(frozen=True, slots=True)
class BeaconDescriptor:
    """One beacon response from an active scan."""

    mac_address: bytes
    pan_id: int
    rssi: int

    @property
    def address(self) -> IPv6Address:
        """Link-local address of the responding device."""
        return link_local_address(self.mac_address)


@dataclass(frozen=True, slots=True)
class ActiveScanResult:
    """A single channel's result from Execute Active Scan (§3.2.3.4)."""

    responded: bool
    channel: int
    beacons: tuple[BeaconDescriptor, ...] = ()


_BEACON_SIZE: Final = 11


def parse_active_scan_notification(frame: Frame) -> ActiveScanResult:
    """Decode an active-scan result notification."""
    data = _require(frame, 2, "a scan result")
    # 0x00 means the channel produced a beacon response, 0x01 means it did not.
    responded = data[0] == 0x00
    channel = data[1]
    if not responded:
        return ActiveScanResult(responded=False, channel=channel)
    count = _require(frame, 3, "a beacon count")[2]
    expected = 3 + count * _BEACON_SIZE
    if len(data) < expected:
        raise FrameFormatError(
            f"scan notification carries {len(data)} bytes, "
            f"too short for {count} beacons ({expected} bytes)"
        )
    beacons = tuple(
        BeaconDescriptor(
            mac_address=bytes(data[offset : offset + 8]),
            pan_id=int.from_bytes(data[offset + 8 : offset + 10], "big"),
            rssi=_rssi(data[offset + 10]),
        )
        for offset in range(3, expected, _BEACON_SIZE)
    )
    return ActiveScanResult(responded=True, channel=channel, beacons=beacons)


@dataclass(frozen=True, slots=True)
class UdpDatagram:
    """A UDP datagram reported by Notify Data Reception (§3.2.5.1)."""

    source: IPv6Address
    source_port: int
    destination_port: int
    source_pan_id: int
    multicast: bool
    encrypted: bool
    rssi: int
    payload: bytes


def parse_data_reception_notification(frame: Frame) -> UdpDatagram:
    """Decode a UDP data reception notification."""
    data = _require(frame, 27, "a UDP reception header")
    size = int.from_bytes(data[25:27], "big")
    payload = bytes(data[27:])
    if len(payload) != size:
        raise FrameFormatError(
            f"UDP reception claims {size} payload bytes but carries {len(payload)}"
        )
    return UdpDatagram(
        source=IPv6Address(bytes(data[0:16])),
        source_port=int.from_bytes(data[16:18], "big"),
        destination_port=int.from_bytes(data[18:20], "big"),
        source_pan_id=int.from_bytes(data[20:22], "big"),
        multicast=data[22] == 0x01,
        encrypted=data[23] == _ENCRYPTED,
        rssi=_rssi(data[24]),
        payload=payload,
    )


@dataclass(frozen=True, slots=True)
class PanaResult:
    """Outcome reported by Notify PANA Authentication Result (§3.2.5.4)."""

    result: PanaResultCode | int
    mac_address: bytes

    @property
    def succeeded(self) -> bool:
        """Whether authentication completed."""
        return self.result == PanaResultCode.SUCCEEDED


def parse_pana_result_notification(frame: Frame) -> PanaResult:
    """Decode a PANA authentication result notification."""
    data = _require(frame, 9, "a PANA result")
    return PanaResult(
        result=_as_enum(PanaResultCode, data[0]), mac_address=bytes(data[1:9])
    )


@dataclass(frozen=True, slots=True)
class ConnectionStatusChange:
    """Change reported by Notify Connection Status Change (§3.2.5.3)."""

    status: AccessPointStatus | int
    mac_address: bytes
    rssi: int

    @property
    def disconnected(self) -> bool:
        """Whether the access point left the MAC or PANA connected state."""
        return self.status in (
            AccessPointStatus.MAC_DISCONNECTED,
            AccessPointStatus.PANA_DISCONNECTED,
        )


def parse_connection_status_notification(frame: Frame) -> ConnectionStatusChange:
    """Decode a connection status change notification."""
    data = _require(frame, 10, "a connection status change")
    return ConnectionStatusChange(
        status=_as_enum(AccessPointStatus, data[0]),
        mac_address=bytes(data[1:9]),
        rssi=_rssi(data[9]),
    )


@dataclass(frozen=True, slots=True)
class ReceptionFailure:
    """Failure reported by Notify Packet Reception Failure (§3.2.5.5)."""

    reason: ReceptionFailureReason | int
    source: IPv6Address
    sequence_number: int
    fragmented: bool
    fragment_tag: int


def parse_reception_failure_notification(frame: Frame) -> ReceptionFailure:
    """Decode a packet reception failure notification."""
    data = _require(frame, 21, "a reception failure")
    return ReceptionFailure(
        reason=_as_enum(ReceptionFailureReason, data[0]),
        source=IPv6Address(bytes(data[1:17])),
        sequence_number=data[17],
        # 0x00 means a fragment is present, 0x01 means there is none.
        fragmented=data[18] == 0x00,
        fragment_tag=int.from_bytes(data[19:21], "big"),
    )


@dataclass(frozen=True, slots=True)
class TransmitResult:
    """Outcome reported by a Transmit Data response (§3.2.3.3)."""

    queue_result: int
    transmission_result: int

    @property
    def queued(self) -> bool:
        """Whether the datagram went into the indirect queue instead of the air."""
        return self.queue_result == 0x1

    @property
    def transmission_succeeded(self) -> bool:
        """Whether the datagram was transmitted."""
        return self.transmission_result == TRANSMISSION_SUCCEEDED

    @property
    def retryable(self) -> bool:
        """Whether resending the datagram can still succeed.

        A meter that misses one frame does not answer its ACK, which the
        adapter reports as a transmission failure even though the link is fine.
        """
        return self.transmission_result in RETRYABLE_TRANSMISSION_RESULTS


def parse_transmit_data_response(frame: Frame) -> TransmitResult:
    """Decode a Transmit Data response."""
    raise_for_result(frame)
    data = _require(frame, 2, "a transmission result")
    return TransmitResult(
        queue_result=data[1] >> 4,
        transmission_result=data[1] & 0x0F,
    )


def _as_enum[EnumT: IntEnum](enum_class: type[EnumT], value: int) -> EnumT | int:
    """Return ``value`` as ``enum_class`` when the specification defines it.

    Unspecified codes are kept as integers so a firmware revision that adds one
    cannot break an otherwise usable session.
    """
    try:
        return enum_class(value)
    except ValueError:
        return value
