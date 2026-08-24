"""Tests for the J11 frame codec."""

from __future__ import annotations

import pytest

from custom_components.broute_j11.protocol.codec import (
    HEADER_SIZE,
    MAX_FRAME_SIZE,
    MAX_MESSAGE_LENGTH,
    REQUEST_UNIQUE_CODE,
    RESPONSE_UNIQUE_CODE,
    ChecksumError,
    Frame,
    FrameFormatError,
    FrameReassembler,
    checksum,
    decode_frame,
    encode_request,
)

from .fixtures import frames

GOLDEN_REQUESTS = [
    (0x00D9, b"", frames.RESET_REQUEST),
    (0x005F, b"\x05\x00\x04\x00", frames.INITIAL_SETTINGS_REQUEST_CHANNEL_4),
    (0x0053, b"", frames.ROUTE_B_START_REQUEST),
    (0x0005, b"\x0e\x1a", frames.UDP_OPEN_REQUEST),
    (0x0056, b"", frames.PANA_START_REQUEST),
]

GOLDEN_INBOUND = [
    frames.STARTUP_NOTIFICATION,
    frames.INITIAL_SETTINGS_RESPONSE,
    frames.CREDENTIALS_RESPONSE,
    frames.ACTIVE_SCAN_NOTIFICATION_EMPTY,
    frames.ACTIVE_SCAN_NOTIFICATION_RESPONSE,
    frames.ROUTE_B_START_RESPONSE,
    frames.PANA_RESULT_NOTIFICATION,
    frames.TRANSMIT_DATA_RESPONSE,
    frames.DATA_RECEPTION_NOTIFICATION,
]


def test_checksum_of_empty_payload_is_zero() -> None:
    assert checksum(b"") == 0


def test_checksum_sums_bytes() -> None:
    assert checksum(b"\x05\x00\x04\x00") == 0x0009


def test_checksum_wraps_at_16_bits() -> None:
    assert checksum(b"\xff" * 257) == (255 * 257) % 0x10000


@pytest.mark.parametrize(("command_code", "data", "expected"), GOLDEN_REQUESTS)
def test_encode_request_matches_documented_frame(
    command_code: int, data: bytes, expected: bytes
) -> None:
    assert encode_request(command_code, data) == expected


def test_encode_request_rejects_oversized_data() -> None:
    with pytest.raises(FrameFormatError):
        encode_request(0x0008, b"\x00" * (MAX_FRAME_SIZE - HEADER_SIZE + 1))


def test_encode_request_rejects_out_of_range_command_code() -> None:
    with pytest.raises(FrameFormatError):
        encode_request(0x1_0000, b"")


def test_frame_roundtrip_preserves_unique_code() -> None:
    frame = Frame(
        command_code=0x0008, data=b"\x01\x02", unique_code=REQUEST_UNIQUE_CODE
    )
    assert decode_frame(frame.encode()) == frame


@pytest.mark.parametrize("raw", GOLDEN_INBOUND)
def test_decode_frame_accepts_documented_frames(raw: bytes) -> None:
    frame = decode_frame(raw)
    assert frame.unique_code == RESPONSE_UNIQUE_CODE
    assert frame.encode() == raw


def test_decode_frame_extracts_fields() -> None:
    frame = decode_frame(frames.INITIAL_SETTINGS_RESPONSE)
    assert frame.command_code == 0x205F
    assert frame.data == b"\x01"
    assert frame.is_response
    assert not frame.is_notification


def test_decode_frame_marks_notifications() -> None:
    frame = decode_frame(frames.STARTUP_NOTIFICATION)
    assert frame.is_notification
    assert not frame.is_response


def test_decode_frame_rejects_short_input() -> None:
    with pytest.raises(FrameFormatError):
        decode_frame(frames.RESET_REQUEST[:-1])


def test_decode_frame_rejects_unknown_unique_code() -> None:
    corrupt = b"\x00\x00\x00\x00" + frames.STARTUP_NOTIFICATION[4:]
    with pytest.raises(FrameFormatError):
        decode_frame(corrupt)


def test_decode_frame_rejects_trailing_bytes() -> None:
    with pytest.raises(FrameFormatError):
        decode_frame(frames.STARTUP_NOTIFICATION + b"\x00")


def test_decode_frame_rejects_bad_header_checksum() -> None:
    corrupt = bytearray(frames.INITIAL_SETTINGS_RESPONSE)
    corrupt[9] ^= 0x01
    with pytest.raises(ChecksumError):
        decode_frame(bytes(corrupt))


def test_decode_frame_rejects_bad_data_checksum() -> None:
    corrupt = bytearray(frames.INITIAL_SETTINGS_RESPONSE)
    corrupt[12] ^= 0x01
    with pytest.raises(ChecksumError):
        decode_frame(bytes(corrupt))


def test_decode_frame_rejects_impossible_message_length() -> None:
    corrupt = bytearray(frames.STARTUP_NOTIFICATION)
    corrupt[6:8] = (0x0003).to_bytes(2, "big")
    corrupt[8:10] = checksum(bytes(corrupt[:8])).to_bytes(2, "big")
    with pytest.raises(FrameFormatError):
        decode_frame(bytes(corrupt))


def test_reassembler_returns_frame_from_single_read() -> None:
    reassembler = FrameReassembler()
    assert [f.command_code for f in reassembler.feed(frames.STARTUP_NOTIFICATION)] == [
        0x6019
    ]


def test_reassembler_assembles_byte_by_byte() -> None:
    reassembler = FrameReassembler()
    collected: list[Frame] = []
    for index in range(len(frames.DATA_RECEPTION_NOTIFICATION)):
        chunk = frames.DATA_RECEPTION_NOTIFICATION[index : index + 1]
        collected.extend(reassembler.feed(chunk))
    assert [f.command_code for f in collected] == [0x6018]
    assert collected[0].encode() == frames.DATA_RECEPTION_NOTIFICATION


def test_reassembler_splits_multiple_frames_in_one_read() -> None:
    reassembler = FrameReassembler()
    stream = (
        frames.STARTUP_NOTIFICATION
        + frames.INITIAL_SETTINGS_RESPONSE
        + frames.DATA_RECEPTION_NOTIFICATION
    )
    assert [f.command_code for f in reassembler.feed(stream)] == [
        0x6019,
        0x205F,
        0x6018,
    ]


def test_reassembler_skips_leading_garbage() -> None:
    reassembler = FrameReassembler()
    result = reassembler.feed(b"\x11\x22\x33" + frames.STARTUP_NOTIFICATION)
    assert [f.command_code for f in result] == [0x6019]
    assert reassembler.stats.discarded_bytes == 3


def test_reassembler_keeps_partial_unique_code_across_reads() -> None:
    reassembler = FrameReassembler()
    assert reassembler.feed(frames.STARTUP_NOTIFICATION[:2]) == []
    assert [
        f.command_code for f in reassembler.feed(frames.STARTUP_NOTIFICATION[2:])
    ] == [0x6019]
    assert reassembler.stats.discarded_bytes == 0


def test_reassembler_recovers_after_data_checksum_error() -> None:
    reassembler = FrameReassembler()
    corrupt = bytearray(frames.INITIAL_SETTINGS_RESPONSE)
    corrupt[12] ^= 0xFF
    result = reassembler.feed(bytes(corrupt) + frames.STARTUP_NOTIFICATION)
    assert [f.command_code for f in result] == [0x6019]
    assert reassembler.stats.checksum_errors == 1


def test_reassembler_recovers_after_header_checksum_error() -> None:
    reassembler = FrameReassembler()
    corrupt = bytearray(frames.INITIAL_SETTINGS_RESPONSE)
    corrupt[8] ^= 0xFF
    result = reassembler.feed(bytes(corrupt) + frames.STARTUP_NOTIFICATION)
    assert [f.command_code for f in result] == [0x6019]
    assert reassembler.stats.checksum_errors == 1


def test_reassembler_recovers_after_impossible_message_length() -> None:
    reassembler = FrameReassembler()
    corrupt = bytearray(frames.STARTUP_NOTIFICATION)
    corrupt[6:8] = (MAX_MESSAGE_LENGTH + 1).to_bytes(2, "big")
    corrupt[8:10] = checksum(bytes(corrupt[:8])).to_bytes(2, "big")
    result = reassembler.feed(bytes(corrupt) + frames.STARTUP_NOTIFICATION)
    assert [f.command_code for f in result] == [0x6019]
    assert reassembler.stats.format_errors == 1


def test_reassembler_accepts_request_unique_code() -> None:
    reassembler = FrameReassembler()
    result = reassembler.feed(frames.RESET_REQUEST)
    assert [f.unique_code for f in result] == [REQUEST_UNIQUE_CODE]


def test_reassembler_bounds_its_buffer() -> None:
    reassembler = FrameReassembler()
    for _ in range(8):
        assert reassembler.feed(b"\x00" * MAX_FRAME_SIZE) == []
    assert reassembler.buffered_bytes <= MAX_FRAME_SIZE
    assert reassembler.stats.discarded_bytes == 8 * MAX_FRAME_SIZE - 3


def test_reassembler_reset_clears_buffer() -> None:
    reassembler = FrameReassembler()
    assert reassembler.feed(frames.STARTUP_NOTIFICATION[:6]) == []
    reassembler.reset()
    assert reassembler.buffered_bytes == 0
    assert reassembler.feed(frames.STARTUP_NOTIFICATION[6:]) == []
