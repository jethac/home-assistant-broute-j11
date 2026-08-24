"""Byte transports for the J11 adapter.

The session layer only ever touches a transport from an executor thread, so the
interface here is deliberately blocking. :class:`SerialTransport` reads with a
short per-call timeout rather than waiting for a whole frame: that keeps every
executor call bounded, so closing the port or cancelling a coordinator refresh
takes effect within one read slice instead of blocking Home Assistant's
shutdown.
"""

from __future__ import annotations

import contextlib
from typing import Final, Protocol, runtime_checkable

import serial

#: Wire settings of the BP35C0-J11 UART interface (specification §2.2).
BAUD_RATE: Final = 115200
#: Longest a single read may block, keeping cancellation responsive.
READ_SLICE_SECONDS: Final = 0.2
#: Bytes to request per read; a whole frame is at most 1,361 bytes.
READ_CHUNK: Final = 1024


class TransportError(Exception):
    """The adapter's byte stream is unusable, for example after a USB unplug.

    The session and config entry layers log these messages, so they never name
    the device: a ``/dev/serial/by-id/...`` path carries the adapter's USB
    serial number.
    """


@runtime_checkable
class ByteTransport(Protocol):
    """A blocking, byte-oriented link to an adapter."""

    def open(self) -> None:
        """Open the link. Raises :class:`TransportError` if it cannot be opened."""

    def close(self) -> None:
        """Close the link. Must be safe to call more than once."""

    def read(self, size: int = READ_CHUNK) -> bytes:
        """Read up to ``size`` bytes, returning ``b""`` when the slice times out."""

    def write(self, data: bytes) -> None:
        """Write ``data`` in full."""


class SerialTransport:
    """A :class:`ByteTransport` backed by pyserial."""

    def __init__(
        self,
        device: str,
        *,
        baud_rate: int = BAUD_RATE,
        read_slice: float = READ_SLICE_SECONDS,
    ) -> None:
        """Prepare a transport for ``device`` without opening it."""
        self._device = device
        self._baud_rate = baud_rate
        self._read_slice = read_slice
        self._port: serial.Serial | None = None

    @property
    def device(self) -> str:
        """The serial device this transport was configured with."""
        return self._device

    def open(self) -> None:
        """Open the port with the adapter's wire settings and flow control off."""
        if self._port is not None:
            return
        try:
            self._port = serial.Serial(
                port=self._device,
                baudrate=self._baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                rtscts=False,
                dsrdtr=False,
                xonxoff=False,
                timeout=self._read_slice,
                write_timeout=self._read_slice * 10,
            )
        except (OSError, serial.SerialException) as err:
            raise TransportError(
                f"the serial device could not be opened ({type(err).__name__})"
            ) from err

    def close(self) -> None:
        """Close the port, ignoring a port that has already gone away."""
        port, self._port = self._port, None
        if port is None:
            return
        # The device disappearing during shutdown is not worth reporting: the
        # caller is tearing the session down anyway.
        with contextlib.suppress(OSError, serial.SerialException):
            port.close()

    def read(self, size: int = READ_CHUNK) -> bytes:
        """Read whatever has arrived within one read slice.

        A blocking one-byte read bounds the wait, and the bytes already
        buffered are drained with it so a burst costs a single executor call.
        """
        port = self._require_port()
        try:
            waiting = port.in_waiting
            return bytes(port.read(min(size, waiting) if waiting else 1))
        except (OSError, serial.SerialException) as err:
            raise TransportError(
                f"reading from the serial device failed ({type(err).__name__})"
            ) from err

    def write(self, data: bytes) -> None:
        """Write ``data`` and flush it to the adapter."""
        port = self._require_port()
        try:
            port.write(data)
            port.flush()
        except (OSError, serial.SerialException) as err:
            raise TransportError(
                f"writing to the serial device failed ({type(err).__name__})"
            ) from err

    def _require_port(self) -> serial.Serial:
        if self._port is None:
            raise TransportError("the serial device is not open")
        return self._port
