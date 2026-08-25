"""ECHONET Lite frames and low-voltage smart-meter properties.

The frame layout follows the ECHONET Lite specification (Part 2, §3.2): a fixed
``0x1081`` header, a 2 byte transaction ID, the 3 byte source and destination
object codes, the service code, the property count and then ``EPC``/``PDC``/
``EDT`` triplets.

Property semantics follow the ECHONET Device Objects specification for the
low-voltage smart electric energy meter class (``0x0288``), Appendix, §3.3.25.
Energy values are handled as :class:`~decimal.Decimal` so a coefficient and a
0.1 kWh unit cannot introduce binary rounding error before Home Assistant
converts them for the statistics table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import IntEnum
from typing import Final, Self

from .codec import ProtocolError

#: ECHONET Lite header for a "format 1" frame.
EHD: Final = 0x1081
#: Controller object this integration presents itself as (class 0x05FF).
CONTROLLER_OBJECT: Final = 0x05FF01
#: Low-voltage smart electric energy meter, instance 1.
LOW_VOLTAGE_METER_OBJECT: Final = 0x028801

HEADER_SIZE: Final = 12
MAX_PROPERTIES: Final = 0xFF
MAX_EDT_SIZE: Final = 0xFF


class Esv(IntEnum):
    """ECHONET Lite service codes used by this integration."""

    SET_I = 0x60
    SET_C = 0x61
    GET = 0x62
    INF_REQ = 0x63
    SET_GET = 0x6E
    SET_RES = 0x71
    GET_RES = 0x72
    INF = 0x73
    INFC = 0x74
    SET_I_SNA = 0x50
    SET_C_SNA = 0x51
    GET_SNA = 0x52


class Epc(IntEnum):
    """Properties of the low-voltage smart electric energy meter class."""

    OPERATION_STATUS = 0x80
    STANDARD_VERSION = 0x82
    MANUFACTURER_CODE = 0x8A
    SERIAL_NUMBER = 0x8D
    COEFFICIENT = 0xD3
    CUMULATIVE_DIGITS = 0xD7
    CUMULATIVE_FORWARD_ENERGY = 0xE0
    CUMULATIVE_UNIT = 0xE1
    CUMULATIVE_REVERSE_ENERGY = 0xE3
    INSTANTANEOUS_POWER = 0xE7
    INSTANTANEOUS_CURRENT = 0xE8
    SCHEDULED_FORWARD_ENERGY = 0xEA
    SCHEDULED_REVERSE_ENERGY = 0xEB


#: Appendix §3.3.25: unit of the cumulative electric energy properties.
CUMULATIVE_UNITS: Final[dict[int, Decimal]] = {
    0x00: Decimal(1),
    0x01: Decimal("0.1"),
    0x02: Decimal("0.01"),
    0x03: Decimal("0.001"),
    0x04: Decimal("0.0001"),
    0x0A: Decimal(10),
    0x0B: Decimal(100),
    0x0C: Decimal(1000),
    0x0D: Decimal(10000),
}

#: Sentinels the meter reports when a value cannot be measured.
_ENERGY_UNAVAILABLE: Final = 0xFFFFFFFE
_POWER_UNAVAILABLE: Final = 0x7FFFFFFE
_CURRENT_UNAVAILABLE: Final = 0x7FFE
#: The current property reports 0.1 A units.
_CURRENT_UNIT: Final = Decimal("0.1")

_ENERGY_SIZE: Final = 4
_POWER_SIZE: Final = 4
_CURRENT_SIZE: Final = 4
_TIMESTAMP_SIZE: Final = 7
_MANUFACTURER_CODE_SIZE: Final = 3
_STANDARD_VERSION_SIZE: Final = 4


class EchonetFrameError(ProtocolError):
    """An ECHONET Lite frame or property value could not be decoded."""


@dataclass(frozen=True, slots=True)
class Property:
    """One EPC/EDT pair. A request carries an empty ``edt``."""

    epc: int
    edt: bytes = b""

    def encode(self) -> bytes:
        """Serialise the property as EPC, PDC and EDT."""
        if len(self.edt) > MAX_EDT_SIZE:
            raise EchonetFrameError(
                f"property 0x{self.epc:02X} carries {len(self.edt)} bytes, "
                f"more than the {MAX_EDT_SIZE} byte maximum"
            )
        return bytes([self.epc, len(self.edt)]) + self.edt


@dataclass(frozen=True, slots=True)
class EchonetLiteFrame:
    """A format 1 ECHONET Lite frame."""

    transaction_id: int
    source_object: int
    destination_object: int
    esv: Esv | int
    properties: tuple[Property, ...] = field(default_factory=tuple)

    @property
    def is_get_response(self) -> bool:
        """Whether the frame answers a Get with property values."""
        return self.esv == Esv.GET_RES

    @property
    def is_from_low_voltage_meter(self) -> bool:
        """Whether the frame came from a low-voltage smart meter object."""
        return self.source_object == LOW_VOLTAGE_METER_OBJECT

    def property_map(self) -> dict[int, bytes]:
        """Return the frame's properties keyed by EPC."""
        return {property.epc: property.edt for property in self.properties}

    def encode(self) -> bytes:
        """Serialise the frame."""
        if not 0 < len(self.properties) <= MAX_PROPERTIES:
            raise ValueError(
                "an ECHONET Lite frame carries at least one property and at "
                f"most {MAX_PROPERTIES} properties, got {len(self.properties)}"
            )
        head = (
            EHD.to_bytes(2, "big")
            + (self.transaction_id & 0xFFFF).to_bytes(2, "big")
            + self.source_object.to_bytes(3, "big")
            + self.destination_object.to_bytes(3, "big")
            + bytes([self.esv, len(self.properties)])
        )
        return head + b"".join(property.encode() for property in self.properties)

    @classmethod
    def decode(cls, payload: bytes) -> Self:
        """Decode ``payload``, rejecting anything that is not a whole frame."""
        if len(payload) < HEADER_SIZE:
            raise EchonetFrameError(
                f"ECHONET Lite frame of {len(payload)} bytes is shorter than "
                f"its {HEADER_SIZE} byte header"
            )
        header = int.from_bytes(payload[0:2], "big")
        if header != EHD:
            raise EchonetFrameError(f"unexpected ECHONET Lite header 0x{header:04X}")
        count = payload[11]
        properties: list[Property] = []
        offset = HEADER_SIZE
        for _ in range(count):
            if offset + 2 > len(payload):
                raise EchonetFrameError(
                    f"frame claims {count} properties but ends after {len(properties)}"
                )
            epc = payload[offset]
            size = payload[offset + 1]
            start = offset + 2
            offset = start + size
            if offset > len(payload):
                raise EchonetFrameError(
                    f"property 0x{epc:02X} claims {size} bytes but the frame "
                    f"holds {len(payload) - start}"
                )
            properties.append(Property(epc=epc, edt=payload[start:offset]))
        if offset != len(payload):
            raise EchonetFrameError(
                f"{len(payload) - offset} trailing bytes after "
                f"{count} ECHONET Lite properties"
            )
        return cls(
            transaction_id=int.from_bytes(payload[2:4], "big"),
            source_object=int.from_bytes(payload[4:7], "big"),
            destination_object=int.from_bytes(payload[7:10], "big"),
            esv=_as_esv(payload[10]),
            properties=tuple(properties),
        )


def _as_esv(value: int) -> Esv | int:
    try:
        return Esv(value)
    except ValueError:
        return value


def encode_get(
    epcs: list[Epc] | list[int],
    *,
    transaction_id: int,
    destination_object: int = LOW_VOLTAGE_METER_OBJECT,
) -> bytes:
    """Encode a Get request for ``epcs``."""
    if not epcs:
        raise ValueError("a Get request needs at least one property")
    if len(epcs) > MAX_PROPERTIES:
        raise ValueError(
            f"a Get request carries at most {MAX_PROPERTIES} properties, "
            f"got {len(epcs)}"
        )
    return EchonetLiteFrame(
        transaction_id=transaction_id,
        source_object=CONTROLLER_OBJECT,
        destination_object=destination_object,
        esv=Esv.GET,
        properties=tuple(Property(epc=epc) for epc in epcs),
    ).encode()


@dataclass(frozen=True, slots=True)
class MeterProfile:
    """Scaling and identity the meter reports once per session.

    The defaults are the specification's own: a coefficient of 1 and a unit of
    1 kWh, which is what a meter that does not implement ``0xD3`` means.
    """

    coefficient: int = 1
    unit: Decimal = Decimal(1)
    digits: int = 6
    manufacturer_code: str | None = None
    standard_version: str | None = None
    serial_number: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduledEnergy:
    """A timestamped cumulative energy reading (``0xEA`` and ``0xEB``)."""

    measured_at: datetime | None
    energy: Decimal


@dataclass(frozen=True, slots=True)
class InstantaneousCurrent:
    """The two phase currents reported by ``0xE8``, in amperes.

    ``t_phase`` is ``None`` on a single-phase two-wire connection, where the
    meter reports the "no measurement" sentinel for that phase.
    """

    r_phase: Decimal | None
    t_phase: Decimal | None


def _require(edt: bytes, size: int, description: str) -> bytes:
    if len(edt) != size:
        raise EchonetFrameError(f"{description} must be {size} bytes, got {len(edt)}")
    return edt


def unit_multiplier(edt: bytes) -> Decimal:
    """Return the kWh multiplier encoded by the cumulative unit property."""
    _require(edt, 1, "the cumulative energy unit")
    try:
        return CUMULATIVE_UNITS[edt[0]]
    except KeyError:
        raise EchonetFrameError(
            f"undocumented cumulative energy unit 0x{edt[0]:02X}"
        ) from None


def decode_coefficient(edt: bytes) -> int:
    """Return the cumulative energy coefficient (``0xD3``)."""
    return int.from_bytes(_require(edt, 4, "the coefficient"), "big")


def decode_cumulative_digits(edt: bytes) -> int:
    """Return the number of significant digits of the energy counters."""
    return _require(edt, 1, "the number of cumulative digits")[0]


def decode_cumulative_energy(edt: bytes, profile: MeterProfile) -> Decimal | None:
    """Return a cumulative energy counter in kWh, or ``None`` if unmeasurable."""
    raw = int.from_bytes(_require(edt, _ENERGY_SIZE, "a cumulative energy"), "big")
    if raw == _ENERGY_UNAVAILABLE:
        return None
    return raw * profile.coefficient * profile.unit


def decode_scheduled_energy(edt: bytes, profile: MeterProfile) -> ScheduledEnergy:
    """Return a timestamped cumulative energy reading in kWh."""
    data = _require(
        edt, _TIMESTAMP_SIZE + _ENERGY_SIZE, "a scheduled cumulative energy"
    )
    energy = int.from_bytes(data[_TIMESTAMP_SIZE:], "big") * profile.coefficient
    return ScheduledEnergy(
        measured_at=_decode_timestamp(data[:_TIMESTAMP_SIZE]),
        energy=energy * profile.unit,
    )


def _decode_timestamp(raw: bytes) -> datetime | None:
    """Decode the meter's local timestamp, or ``None`` if it is not set.

    The meter reports local wall-clock time without an offset, so the value is
    returned naive and is only used for diagnostics.
    """
    try:
        return datetime(
            year=int.from_bytes(raw[0:2], "big"),
            month=raw[2],
            day=raw[3],
            hour=raw[4],
            minute=raw[5],
            second=raw[6],
        )
    except ValueError:
        return None


def decode_instantaneous_power(edt: bytes) -> int | None:
    """Return instantaneous power in W, or ``None`` if unmeasurable."""
    data = _require(edt, _POWER_SIZE, "the instantaneous power")
    if int.from_bytes(data, "big") == _POWER_UNAVAILABLE:
        return None
    return int.from_bytes(data, "big", signed=True)


def decode_instantaneous_current(edt: bytes) -> InstantaneousCurrent:
    """Return both phase currents in A, with unmeasurable phases as ``None``."""
    data = _require(edt, _CURRENT_SIZE, "the instantaneous current")
    return InstantaneousCurrent(
        r_phase=_decode_phase_current(data[0:2]),
        t_phase=_decode_phase_current(data[2:4]),
    )


def _decode_phase_current(raw: bytes) -> Decimal | None:
    if int.from_bytes(raw, "big") == _CURRENT_UNAVAILABLE:
        return None
    return int.from_bytes(raw, "big", signed=True) * _CURRENT_UNIT


def decode_manufacturer_code(edt: bytes) -> str:
    """Return the ECHONET manufacturer code as a hexadecimal string."""
    data = _require(edt, _MANUFACTURER_CODE_SIZE, "the manufacturer code")
    return f"0x{data.hex().upper()}"


def decode_protocol_version(edt: bytes) -> str:
    """Return the standard version property (``0x82``) as a hexadecimal string."""
    data = _require(edt, _STANDARD_VERSION_SIZE, "the standard version")
    return f"0x{data.hex().upper()}"


def decode_serial_number(edt: bytes) -> str:
    """Return the meter's production number.

    The value identifies a specific meter, so callers must redact it before it
    reaches diagnostics or logs.
    """
    return edt.decode("ascii", errors="replace").strip().rstrip("\x00")
