"""An in-memory BP35C0-J11 adapter and smart meter.

The fake speaks the real binary protocol over the same blocking transport
interface the pyserial implementation exposes, so session tests exercise the
production framing, routing and lifecycle code instead of mocks. Failure modes
(no beacon, rejected credentials, a silent adapter, a vanished USB device,
line noise) are configuration, not patching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import threading
from typing import Final

from custom_components.broute_j11.protocol.codec import (
    RESPONSE_UNIQUE_CODE,
    Frame,
    FrameReassembler,
    response_code,
)
from custom_components.broute_j11.protocol.commands import (
    ALL_CHANNELS_MASK,
    CommandCode,
    NotificationCode,
)
from custom_components.broute_j11.protocol.echonet import (
    CONTROLLER_OBJECT,
    LOW_VOLTAGE_METER_OBJECT,
    EchonetLiteFrame,
    Epc,
    Esv,
    Property,
)
from custom_components.broute_j11.protocol.transport import TransportError

#: Meter identity used by ROHM's own documentation samples.
METER_MAC: Final = bytes.fromhex("0050C2FFFEDC2822")
METER_ADDRESS: Final = bytes.fromhex("FE80000000000000") + bytes.fromhex(
    "0250C2FFFEDC2822"
)
METER_PAN_ID: Final = 0xBCDE
METER_CHANNEL: Final = 0x0C
METER_RSSI_BYTE: Final = 0xDE

_SUCCESS: Final = 0x01
_FAILED: Final = 0x02
#: Response result the adapter returns when the credentials were already set.
CREDENTIALS_ALREADY_SET: Final = 0x58

DEFAULT_PROPERTIES: Final[dict[int, bytes]] = {
    Epc.COEFFICIENT: (1).to_bytes(4, "big"),
    Epc.CUMULATIVE_DIGITS: bytes([6]),
    Epc.CUMULATIVE_UNIT: bytes([0x01]),
    Epc.MANUFACTURER_CODE: bytes.fromhex("00000B"),
    Epc.STANDARD_VERSION: bytes.fromhex("00004600"),
    Epc.SERIAL_NUMBER: b"SYNTHETIC001",
    Epc.INSTANTANEOUS_POWER: (1234).to_bytes(4, "big"),
    Epc.INSTANTANEOUS_CURRENT: (56).to_bytes(2, "big") + (78).to_bytes(2, "big"),
    Epc.CUMULATIVE_FORWARD_ENERGY: (226223).to_bytes(4, "big"),
    Epc.CUMULATIVE_REVERSE_ENERGY: (9302).to_bytes(4, "big"),
}

#: Physical values the defaults above decode to, for test assertions.
EXPECTED_POWER: Final = 1234
EXPECTED_CURRENT_R: Final = Decimal("5.6")
EXPECTED_CURRENT_T: Final = Decimal("7.8")
EXPECTED_FORWARD_KWH: Final = Decimal("22622.3")
EXPECTED_REVERSE_KWH: Final = Decimal("930.2")


@dataclass
class AdapterBehaviour:
    """How the fake adapter and meter should misbehave, if at all."""

    auth_id: str = ""
    password: str = ""
    beacon_channel: int | None = METER_CHANNEL
    #: Scans that stay silent before the meter answers, as a short dwell does.
    silent_scans: int = 0
    channel_mask: int = ALL_CHANNELS_MASK
    #: Route-B start requests that fail before one succeeds.
    route_b_failures: int = 0
    pana_result: int = _SUCCESS
    credentials_result: int = _SUCCESS
    fail_open: bool = False
    fail_read_after: int | None = None
    silent_commands: set[int] = field(default_factory=set)
    silent_notifications: set[int] = field(default_factory=set)
    line_noise: bytes = b""
    send_instance_list: bool = False
    get_sna: bool = False
    #: Properties the meter refuses, answering Get_SNA with an empty value.
    unsupported: set[int] = field(default_factory=set)
    drop_meter_responses: int = 0
    properties: dict[int, bytes] = field(
        default_factory=lambda: dict(DEFAULT_PROPERTIES)
    )


class FakeAdapter:
    """A blocking byte transport that answers like a real J11 adapter."""

    def __init__(self, behaviour: AdapterBehaviour | None = None) -> None:
        """Create a closed fake adapter."""
        self.behaviour = behaviour or AdapterBehaviour()
        self.requests: list[Frame] = []
        self.opens = 0
        self.closes = 0
        self.reads = 0
        self._condition = threading.Condition()
        self._outbox = bytearray()
        self._reassembler = FrameReassembler()
        self._open = False
        self._dropped = 0
        self._scans = 0
        self._route_b_attempts = 0

    # -- transport interface -------------------------------------------------

    def open(self) -> None:
        """Open the fake port."""
        if self.behaviour.fail_open:
            raise TransportError("no such device")
        self.opens += 1
        with self._condition:
            self._open = True
            self._outbox.clear()
            if self.behaviour.line_noise:
                self._outbox += self.behaviour.line_noise
            self._condition.notify_all()

    def close(self) -> None:
        """Close the fake port and release a blocked reader."""
        self.closes += 1
        with self._condition:
            self._open = False
            self._condition.notify_all()

    def read(self, size: int = 1024) -> bytes:
        """Return buffered bytes, blocking briefly when there are none."""
        with self._condition:
            self.reads += 1
            if (
                self.behaviour.fail_read_after is not None
                and self.reads > self.behaviour.fail_read_after
            ):
                raise TransportError("device disconnected")
            if not self._outbox:
                self._condition.wait(0.02)
            chunk = bytes(self._outbox[:size])
            del self._outbox[:size]
            return chunk

    def write(self, data: bytes) -> None:
        """Consume host requests and queue whatever the adapter would answer."""
        with self._condition:
            if not self._open:
                raise TransportError("device is not open")
        for frame in self._reassembler.feed(data):
            self.requests.append(frame)
            self._handle(frame)

    # -- test helpers --------------------------------------------------------

    def emit(self, raw: bytes) -> None:
        """Push raw bytes towards the host."""
        with self._condition:
            self._outbox += raw
            self._condition.notify_all()

    def emit_connection_lost(self) -> None:
        """Notify the host that the meter left the connected state."""
        self.notify(
            NotificationCode.CONNECTION_STATUS,
            bytes([0x04]) + METER_MAC + bytes([METER_RSSI_BYTE]),
        )

    def notify(self, code: int, data: bytes = b"") -> None:
        """Push a notification frame, unless the behaviour suppresses it."""
        if code in self.behaviour.silent_notifications:
            return
        self.emit(
            Frame(
                command_code=code, data=data, unique_code=RESPONSE_UNIQUE_CODE
            ).encode()
        )

    def respond(self, command: int, data: bytes) -> None:
        """Push a response frame, unless the behaviour suppresses it."""
        if command in self.behaviour.silent_commands:
            return
        self.emit(
            Frame(
                command_code=response_code(command),
                data=data,
                unique_code=RESPONSE_UNIQUE_CODE,
            ).encode()
        )

    def sent(self, command: int) -> list[Frame]:
        """Return every request the host sent for ``command``."""
        return [frame for frame in self.requests if frame.command_code == command]

    # -- protocol behaviour --------------------------------------------------

    def _handle(self, frame: Frame) -> None:
        handlers = {
            CommandCode.RESET: self._handle_reset,
            CommandCode.GET_VERSION: self._handle_version,
            CommandCode.SET_INITIAL_SETTINGS: self._handle_initial_settings,
            CommandCode.SET_CREDENTIALS: self._handle_credentials,
            CommandCode.ACTIVE_SCAN: self._handle_scan,
            CommandCode.START_ROUTE_B: self._handle_route_b,
            CommandCode.OPEN_UDP_PORT: self._handle_open_port,
            CommandCode.START_PANA: self._handle_start_pana,
            CommandCode.TERMINATE_PANA: self._handle_terminate_pana,
            CommandCode.TRANSMIT_DATA: self._handle_transmit,
        }
        handler = handlers.get(CommandCode(frame.command_code))
        if handler is not None:
            handler(frame)

    def _handle_reset(self, frame: Frame) -> None:
        self._reassembler.reset()
        self.notify(NotificationCode.STARTUP_COMPLETED)

    def _handle_version(self, frame: Frame) -> None:
        self.respond(
            frame.command_code,
            bytes([_SUCCESS])
            + (0x1234).to_bytes(2, "big")
            + bytes([1, 2])
            + (3).to_bytes(4, "big"),
        )

    def _handle_initial_settings(self, frame: Frame) -> None:
        self.respond(frame.command_code, bytes([_SUCCESS]))

    def _handle_credentials(self, frame: Frame) -> None:
        expected = self.behaviour.auth_id.encode(
            "ascii"
        ) + self.behaviour.password.encode("ascii")
        result = self.behaviour.credentials_result
        if self.behaviour.auth_id and frame.data != expected:
            result = 0x04
        self.respond(frame.command_code, bytes([result]))

    def _handle_scan(self, frame: Frame) -> None:
        self.respond(frame.command_code, bytes([_SUCCESS]))
        self._scans += 1
        answers = self._scans > self.behaviour.silent_scans
        mask = int.from_bytes(frame.data[1:5], "big")
        for channel in sorted(bit for bit in range(32) if mask >> bit & 1):
            if answers and channel == self.behaviour.beacon_channel:
                self.notify(
                    NotificationCode.ACTIVE_SCAN_RESULT,
                    bytes([0x00, channel, 0x01])
                    + METER_MAC
                    + METER_PAN_ID.to_bytes(2, "big")
                    + bytes([METER_RSSI_BYTE]),
                )
                return
            self.notify(NotificationCode.ACTIVE_SCAN_RESULT, bytes([0x01, channel]))

    def _handle_route_b(self, frame: Frame) -> None:
        self._route_b_attempts += 1
        if self._route_b_attempts <= self.behaviour.route_b_failures:
            self.respond(frame.command_code, bytes([_FAILED]))
            return
        self.respond(
            frame.command_code,
            bytes([_SUCCESS, METER_CHANNEL])
            + METER_PAN_ID.to_bytes(2, "big")
            + METER_MAC
            + bytes([METER_RSSI_BYTE]),
        )

    def _handle_open_port(self, frame: Frame) -> None:
        self.respond(frame.command_code, bytes([_SUCCESS]))

    def _handle_start_pana(self, frame: Frame) -> None:
        self.respond(frame.command_code, bytes([_SUCCESS]))
        self.notify(
            NotificationCode.PANA_RESULT,
            bytes([self.behaviour.pana_result]) + METER_MAC,
        )
        if self.behaviour.send_instance_list:
            self._notify_datagram(
                EchonetLiteFrame(
                    transaction_id=1,
                    source_object=LOW_VOLTAGE_METER_OBJECT,
                    destination_object=CONTROLLER_OBJECT,
                    esv=Esv.INF,
                    properties=(Property(epc=Epc.OPERATION_STATUS, edt=b"\x30"),),
                ).encode()
            )

    def _handle_terminate_pana(self, frame: Frame) -> None:
        self.respond(frame.command_code, bytes([_SUCCESS]))

    def _handle_transmit(self, frame: Frame) -> None:
        payload = frame.data[22:]
        self.respond(frame.command_code, bytes([_SUCCESS, 0x00]) + payload[:5])
        if self._dropped < self.behaviour.drop_meter_responses:
            self._dropped += 1
            return
        request = EchonetLiteFrame.decode(payload)
        requested = tuple(request.property_map())
        refused = {
            epc
            for epc in requested
            if self.behaviour.get_sna or epc in self.behaviour.unsupported
        }
        properties = tuple(
            Property(epc=epc, edt=b"" if epc in refused else self._value(epc))
            for epc in requested
        )
        self._notify_datagram(
            EchonetLiteFrame(
                transaction_id=request.transaction_id,
                source_object=LOW_VOLTAGE_METER_OBJECT,
                destination_object=CONTROLLER_OBJECT,
                esv=Esv.GET_SNA if refused else Esv.GET_RES,
                properties=properties,
            ).encode()
        )

    def _value(self, epc: int) -> bytes:
        return self.behaviour.properties.get(epc, b"")

    def _notify_datagram(self, payload: bytes) -> None:
        header = (
            METER_ADDRESS
            + (3610).to_bytes(2, "big")
            + (3610).to_bytes(2, "big")
            + METER_PAN_ID.to_bytes(2, "big")
            + bytes([0x00, 0x02, METER_RSSI_BYTE])
            + len(payload).to_bytes(2, "big")
        )
        self.notify(NotificationCode.DATA_RECEPTION, header + payload)
