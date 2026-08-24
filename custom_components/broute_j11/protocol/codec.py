"""Framing for the BP35C0-J11 binary UART protocol.

The wire format is defined in ROHM's "BP35C0-J11 UART IF specification"
(No. 63TR008E). Every message is big-endian and carries a 12 byte header::

    0..4    unique code (request or response/notification)
    4..6    command code
    6..8    message length: 4 + len(data)
    8..10   header checksum: sum of bytes 0..8, modulo 0x10000
    10..12  data checksum: sum of the data bytes, modulo 0x10000
    12..    data

Nothing in this module logs or embeds payload bytes: frames carry
authentication credentials and meter identifiers, so error messages describe
only sizes and command codes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

REQUEST_UNIQUE_CODE: Final = 0xD0EA83FC
RESPONSE_UNIQUE_CODE: Final = 0xD0F9EE5D
UNIQUE_CODES: Final = (REQUEST_UNIQUE_CODE, RESPONSE_UNIQUE_CODE)

HEADER_SIZE: Final = 12
#: The header's message length field counts the two checksums plus the data.
MESSAGE_LENGTH_OVERHEAD: Final = 4
#: Largest message the module accepts, including the header (specification §3).
MAX_FRAME_SIZE: Final = 1361
MAX_DATA_SIZE: Final = MAX_FRAME_SIZE - HEADER_SIZE
MAX_MESSAGE_LENGTH: Final = MAX_DATA_SIZE + MESSAGE_LENGTH_OVERHEAD
MAX_COMMAND_CODE: Final = 0xFFFF

_CHECKSUM_MODULUS: Final = 0x10000
_UNIQUE_CODE_BYTES: Final = tuple(
    code.to_bytes(4, "big") for code in (REQUEST_UNIQUE_CODE, RESPONSE_UNIQUE_CODE)
)

#: Response command codes are the request code with bit 13 set, notifications
#: have bits 13 and 14 set.
_RESPONSE_FLAG: Final = 0x2000
_NOTIFICATION_FLAG: Final = 0x6000
_CATEGORY_MASK: Final = 0xE000


class ProtocolError(Exception):
    """Base class for J11 protocol failures."""


class FrameFormatError(ProtocolError):
    """A frame has an unusable header, length or size."""


class ChecksumError(ProtocolError):
    """A frame's header or data checksum does not match its contents."""


def checksum(payload: bytes) -> int:
    """Return the J11 checksum of ``payload``."""
    return sum(payload) % _CHECKSUM_MODULUS


@dataclass(frozen=True, slots=True)
class Frame:
    """A decoded J11 message."""

    command_code: int
    data: bytes = b""
    unique_code: int = RESPONSE_UNIQUE_CODE

    @property
    def is_response(self) -> bool:
        """Whether the frame answers a request the host sent."""
        return self.command_code & _CATEGORY_MASK == _RESPONSE_FLAG

    @property
    def is_notification(self) -> bool:
        """Whether the frame was sent spontaneously by the adapter."""
        return self.command_code & _CATEGORY_MASK == _NOTIFICATION_FLAG

    def encode(self) -> bytes:
        """Serialise the frame, computing both checksums."""
        _validate_encodable(self.command_code, self.data, self.unique_code)
        message_length = len(self.data) + MESSAGE_LENGTH_OVERHEAD
        header = (
            self.unique_code.to_bytes(4, "big")
            + self.command_code.to_bytes(2, "big")
            + message_length.to_bytes(2, "big")
        )
        return (
            header
            + checksum(header).to_bytes(2, "big")
            + checksum(self.data).to_bytes(2, "big")
            + self.data
        )


def _validate_encodable(command_code: int, data: bytes, unique_code: int) -> None:
    if unique_code not in UNIQUE_CODES:
        raise FrameFormatError(f"unknown unique code 0x{unique_code:08X}")
    if not 0 <= command_code <= MAX_COMMAND_CODE:
        raise FrameFormatError(f"command code out of range: {command_code}")
    if len(data) > MAX_DATA_SIZE:
        raise FrameFormatError(
            f"data block of {len(data)} bytes exceeds the {MAX_DATA_SIZE} byte limit"
        )


def encode_request(command_code: int, data: bytes = b"") -> bytes:
    """Serialise a host request for ``command_code``."""
    return Frame(
        command_code=command_code, data=data, unique_code=REQUEST_UNIQUE_CODE
    ).encode()


def decode_frame(raw: bytes) -> Frame:
    """Decode exactly one frame, rejecting anything left over.

    Raises:
        FrameFormatError: the buffer is not a single well-formed frame.
        ChecksumError: a checksum does not match the frame contents.

    """
    frame, consumed = _decode_prefix(raw)
    if frame is None:
        raise FrameFormatError(f"incomplete frame of {len(raw)} bytes")
    if consumed != len(raw):
        raise FrameFormatError(f"{len(raw) - consumed} trailing bytes after frame")
    return frame


def _decode_prefix(raw: bytes) -> tuple[Frame | None, int]:
    """Decode the frame starting at offset 0, or report that more data is needed."""
    if len(raw) < HEADER_SIZE:
        return None, 0
    unique_code = int.from_bytes(raw[0:4], "big")
    if unique_code not in UNIQUE_CODES:
        raise FrameFormatError(f"unknown unique code 0x{unique_code:08X}")
    message_length = int.from_bytes(raw[6:8], "big")
    if not MESSAGE_LENGTH_OVERHEAD <= message_length <= MAX_MESSAGE_LENGTH:
        raise FrameFormatError(f"impossible message length {message_length}")
    if checksum(raw[0:8]) != int.from_bytes(raw[8:10], "big"):
        raise ChecksumError("header checksum mismatch")
    total = 8 + message_length
    if len(raw) < total:
        return None, 0
    data = raw[HEADER_SIZE:total]
    if checksum(data) != int.from_bytes(raw[10:12], "big"):
        raise ChecksumError("data checksum mismatch")
    command_code = int.from_bytes(raw[4:6], "big")
    return Frame(command_code=command_code, data=data, unique_code=unique_code), total


@dataclass(slots=True)
class FrameStats:
    """Counters describing what a reassembler had to throw away."""

    frames: int = 0
    discarded_bytes: int = 0
    checksum_errors: int = 0
    format_errors: int = 0


@dataclass(slots=True)
class FrameReassembler:
    """Turns an arbitrarily chunked byte stream into whole frames.

    The reassembler never raises: a corrupt or unsynchronised stream is
    resynchronised on the next unique code and accounted for in :attr:`stats`,
    because a live UART link must survive a truncated read without tearing down
    the session.
    """

    max_buffer_size: int = MAX_FRAME_SIZE
    stats: FrameStats = field(default_factory=FrameStats)
    _buffer: bytearray = field(default_factory=bytearray, repr=False)

    @property
    def buffered_bytes(self) -> int:
        """Number of bytes held back waiting for the rest of a frame."""
        return len(self._buffer)

    def reset(self) -> None:
        """Drop buffered bytes, for example after reopening the port."""
        self._buffer.clear()

    def feed(self, chunk: bytes) -> list[Frame]:
        """Add ``chunk`` to the buffer and return every frame it completes."""
        self._buffer += chunk
        found: list[Frame] = []
        while True:
            self._resync()
            if len(self._buffer) < HEADER_SIZE:
                break
            try:
                frame, consumed = _decode_prefix(bytes(self._buffer))
            except ChecksumError:
                self.stats.checksum_errors += 1
                self._discard(1)
                continue
            except FrameFormatError:
                self.stats.format_errors += 1
                self._discard(1)
                continue
            if frame is None:
                break
            del self._buffer[:consumed]
            self.stats.frames += 1
            found.append(frame)
        self._enforce_limit()
        return found

    def _discard(self, count: int) -> None:
        del self._buffer[:count]
        self.stats.discarded_bytes += count

    def _resync(self) -> None:
        offsets = [
            offset
            for offset in (self._buffer.find(code) for code in _UNIQUE_CODE_BYTES)
            if offset >= 0
        ]
        if offsets:
            self._discard(min(offsets))
            return
        # No complete unique code yet: keep only the bytes that could still be
        # the start of one so a frame split across reads survives.
        self._discard(max(len(self._buffer) - 3, 0))

    def _enforce_limit(self) -> None:
        # A whole frame is at most MAX_FRAME_SIZE bytes, so anything longer than
        # the limit cannot be the prefix of a frame this reassembler will emit.
        if len(self._buffer) > self.max_buffer_size:
            self._discard(len(self._buffer) - self.max_buffer_size)
