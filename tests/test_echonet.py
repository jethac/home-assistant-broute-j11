"""Tests for ECHONET Lite encoding, parsing and unit conversion."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from custom_components.broute_j11.protocol.echonet import (
    CONTROLLER_OBJECT,
    LOW_VOLTAGE_METER_OBJECT,
    EchonetFrameError,
    EchonetLiteFrame,
    Epc,
    Esv,
    MeterProfile,
    Property,
    decode_cumulative_energy,
    decode_instantaneous_current,
    decode_instantaneous_power,
    decode_manufacturer_code,
    decode_protocol_version,
    decode_scheduled_energy,
    decode_serial_number,
    encode_get,
    unit_multiplier,
)

from .fixtures import frames

GET_PAYLOAD = frames.TRANSMIT_DATA_REQUEST[-16:]
GET_RES_PAYLOAD = frames.DATA_RECEPTION_NOTIFICATION[-38:]
INSTANCE_LIST_PAYLOAD = frames.INSTANCE_LIST_NOTIFICATION[-25:]


def test_encode_get_matches_documented_payload() -> None:
    assert (
        encode_get(
            [Epc.SCHEDULED_FORWARD_ENERGY, Epc.SCHEDULED_REVERSE_ENERGY],
            transaction_id=6,
        )
        == GET_PAYLOAD
    )


def test_encode_get_rejects_an_empty_property_list() -> None:
    with pytest.raises(ValueError, match="at least one property"):
        encode_get([], transaction_id=1)


def test_encode_get_rejects_too_many_properties() -> None:
    with pytest.raises(ValueError, match="properties"):
        encode_get([Epc.INSTANTANEOUS_POWER] * 256, transaction_id=1)


def test_encode_get_wraps_the_transaction_id() -> None:
    frame = EchonetLiteFrame.decode(
        encode_get([Epc.COEFFICIENT], transaction_id=0x1_0001)
    )
    assert frame.transaction_id == 1


def test_decode_get_request() -> None:
    frame = EchonetLiteFrame.decode(GET_PAYLOAD)
    assert frame.transaction_id == 6
    assert frame.source_object == CONTROLLER_OBJECT
    assert frame.destination_object == LOW_VOLTAGE_METER_OBJECT
    assert frame.esv is Esv.GET
    assert [property.epc for property in frame.properties] == [0xEA, 0xEB]
    assert all(property.edt == b"" for property in frame.properties)


def test_decode_get_response() -> None:
    frame = EchonetLiteFrame.decode(GET_RES_PAYLOAD)
    assert frame.transaction_id == 6
    assert frame.source_object == LOW_VOLTAGE_METER_OBJECT
    assert frame.destination_object == CONTROLLER_OBJECT
    assert frame.esv is Esv.GET_RES
    assert frame.encode() == GET_RES_PAYLOAD
    assert frame.is_from_low_voltage_meter


def test_decode_notification_frame() -> None:
    frame = EchonetLiteFrame.decode(INSTANCE_LIST_PAYLOAD)
    assert frame.esv is Esv.INF
    assert frame.is_from_low_voltage_meter


def test_frame_property_lookup() -> None:
    frame = EchonetLiteFrame.decode(GET_RES_PAYLOAD)
    assert frame.property_map().keys() == {0xEA, 0xEB}
    assert frame.property_map()[0xEA][:2] == b"\x07\xe2"


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x10\x81\x00\x06",
        b"\x10\x82" + GET_PAYLOAD[2:],
        GET_PAYLOAD[:-1],
        GET_PAYLOAD + b"\x00",
    ],
)
def test_decode_rejects_malformed_payloads(payload: bytes) -> None:
    with pytest.raises(EchonetFrameError):
        EchonetLiteFrame.decode(payload)


def test_decode_rejects_a_property_that_overruns_the_payload() -> None:
    payload = bytes.fromhex("1081000605FF010288016201EA05") + b"\x00\x01"
    with pytest.raises(EchonetFrameError):
        EchonetLiteFrame.decode(payload)


def test_decode_keeps_unknown_service_codes() -> None:
    payload = bytes.fromhex("1081000602880105FF0151 01 E700")
    frame = EchonetLiteFrame.decode(payload)
    assert frame.esv == 0x51
    assert not frame.is_get_response


def test_get_sna_is_reported_as_a_failed_get() -> None:
    payload = bytes.fromhex("1081000602880105FF0152 01 E700")
    frame = EchonetLiteFrame.decode(payload)
    assert frame.esv is Esv.GET_SNA
    assert not frame.is_get_response


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0x00, Decimal(1)),
        (0x01, Decimal("0.1")),
        (0x02, Decimal("0.01")),
        (0x03, Decimal("0.001")),
        (0x04, Decimal("0.0001")),
        (0x0A, Decimal(10)),
        (0x0B, Decimal(100)),
        (0x0C, Decimal(1000)),
        (0x0D, Decimal(10000)),
    ],
)
def test_unit_multiplier_covers_every_documented_unit(
    raw: int, expected: Decimal
) -> None:
    assert unit_multiplier(bytes([raw])) == expected


@pytest.mark.parametrize("raw", [b"", b"\x05", b"\x0e", b"\x01\x02"])
def test_unit_multiplier_rejects_undocumented_units(raw: bytes) -> None:
    with pytest.raises(EchonetFrameError):
        unit_multiplier(raw)


def test_decode_cumulative_energy_applies_coefficient_and_unit() -> None:
    profile = MeterProfile(coefficient=3, unit=Decimal("0.1"), digits=6)
    assert decode_cumulative_energy((12345).to_bytes(4, "big"), profile) == Decimal(
        "3703.5"
    )


def test_decode_cumulative_energy_defaults_to_a_coefficient_of_one() -> None:
    profile = MeterProfile()
    assert decode_cumulative_energy((99999999).to_bytes(4, "big"), profile) == Decimal(
        99999999
    )


def test_decode_cumulative_energy_treats_the_error_value_as_unavailable() -> None:
    assert decode_cumulative_energy(b"\xff\xff\xff\xfe", MeterProfile()) is None


def test_decode_cumulative_energy_rejects_a_short_value() -> None:
    with pytest.raises(EchonetFrameError):
        decode_cumulative_energy(b"\x00\x01", MeterProfile())


def test_decode_scheduled_energy() -> None:
    frame = EchonetLiteFrame.decode(GET_RES_PAYLOAD)
    profile = MeterProfile(coefficient=1, unit=Decimal("0.1"), digits=6)
    reading = decode_scheduled_energy(
        frame.property_map()[Epc.SCHEDULED_FORWARD_ENERGY], profile
    )
    assert reading.measured_at == datetime(2018, 10, 2, 14, 30, 0)
    assert reading.energy == Decimal("22622.3")


def test_decode_scheduled_energy_rejects_a_short_value() -> None:
    with pytest.raises(EchonetFrameError):
        decode_scheduled_energy(b"\x07\xe2", MeterProfile())


def test_decode_scheduled_energy_reports_an_invalid_timestamp_as_none() -> None:
    # Month and day zero: the meter's clock has never been set.
    raw = bytes.fromhex("07E2 00 00 00 00 00") + (10).to_bytes(4, "big")
    reading = decode_scheduled_energy(raw, MeterProfile())
    assert reading.measured_at is None
    assert reading.energy == Decimal(10)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ((512).to_bytes(4, "big"), 512),
        ((-512 & 0xFFFFFFFF).to_bytes(4, "big"), -512),
        (b"\x00\x00\x00\x00", 0),
    ],
)
def test_decode_instantaneous_power_is_signed(raw: bytes, expected: int) -> None:
    assert decode_instantaneous_power(raw) == expected


def test_decode_instantaneous_power_treats_the_error_value_as_unavailable() -> None:
    assert decode_instantaneous_power(b"\x7f\xff\xff\xfe") is None


def test_decode_instantaneous_power_rejects_a_short_value() -> None:
    with pytest.raises(EchonetFrameError):
        decode_instantaneous_power(b"\x00\x00")


def test_decode_instantaneous_current_scales_to_amperes() -> None:
    raw = (123).to_bytes(2, "big") + (-45 & 0xFFFF).to_bytes(2, "big")
    current = decode_instantaneous_current(raw)
    assert current.r_phase == Decimal("12.3")
    assert current.t_phase == Decimal("-4.5")


def test_decode_instantaneous_current_reports_single_phase_meters() -> None:
    raw = (123).to_bytes(2, "big") + b"\x7f\xfe"
    current = decode_instantaneous_current(raw)
    assert current.r_phase == Decimal("12.3")
    assert current.t_phase is None


def test_decode_instantaneous_current_reports_an_unmeasurable_r_phase() -> None:
    current = decode_instantaneous_current(b"\x7f\xfe\x7f\xfe")
    assert current.r_phase is None
    assert current.t_phase is None


def test_decode_instantaneous_current_rejects_a_short_value() -> None:
    with pytest.raises(EchonetFrameError):
        decode_instantaneous_current(b"\x00\x01")


def test_decode_identification_properties() -> None:
    assert decode_manufacturer_code(b"\x00\x00\x0b") == "0x00000B"
    assert decode_protocol_version(b"\x00\x00\x46\x00") == "0x00004600"
    assert decode_serial_number(b"12345678\x00\x00\x00\x00") == "12345678"


@pytest.mark.parametrize("decoder", [decode_manufacturer_code, decode_protocol_version])
def test_identification_decoders_reject_short_values(
    decoder: object,
) -> None:
    with pytest.raises(EchonetFrameError):
        decoder(b"\x00")  # type: ignore[operator]


def test_property_rejects_an_oversized_value() -> None:
    with pytest.raises(EchonetFrameError):
        Property(epc=Epc.COEFFICIENT, edt=b"\x00" * 256).encode()
