"""Tests for the blocking pyserial transport.

pyserial is replaced by a stub port so the wire settings, the bounded read
slice and the error translation are all exercised without hardware.
"""

from __future__ import annotations

import pytest
import serial

from custom_components.broute_j11.protocol import transport as transport_module
from custom_components.broute_j11.protocol.transport import (
    BAUD_RATE,
    ByteTransport,
    SerialTransport,
    TransportError,
)

DEVICE = "/dev/serial/by-id/synthetic-adapter"


class StubPort:
    """A minimal stand-in for :class:`serial.Serial`."""

    def __init__(self, **settings: object) -> None:
        """Create an open, empty stub port."""
        self.settings = settings
        self.buffer = bytearray()
        self.written = bytearray()
        self.flushes = 0
        self.closed = False
        self.fail_on: set[str] = set()

    @property
    def in_waiting(self) -> int:
        """Return how many bytes the adapter has already sent."""
        if "in_waiting" in self.fail_on:
            raise serial.SerialException("device reports no status")
        return len(self.buffer)

    def read(self, size: int = 1) -> bytes:
        """Return up to ``size`` buffered bytes."""
        if "read" in self.fail_on:
            raise serial.SerialException("device disappeared")
        chunk = bytes(self.buffer[:size])
        del self.buffer[:size]
        return chunk

    def write(self, data: bytes) -> int:
        """Record ``data`` as written to the adapter."""
        if "write" in self.fail_on:
            raise serial.SerialException("device disappeared")
        self.written += data
        return len(data)

    def flush(self) -> None:
        """Count a flush."""
        self.flushes += 1

    def close(self) -> None:
        """Mark the port closed."""
        if "close" in self.fail_on:
            raise serial.SerialException("device already gone")
        self.closed = True


@pytest.fixture
def port(monkeypatch: pytest.MonkeyPatch) -> StubPort:
    """Install a stub port and return it."""
    stub = StubPort()

    def factory(**settings: object) -> StubPort:
        stub.settings = settings
        return stub

    monkeypatch.setattr(transport_module.serial, "Serial", factory)
    return stub


def test_the_transport_satisfies_the_protocol() -> None:
    assert isinstance(SerialTransport(DEVICE), ByteTransport)


def test_open_uses_the_documented_wire_settings(port: StubPort) -> None:
    transport = SerialTransport(DEVICE)
    transport.open()
    assert transport.device == DEVICE
    assert port.settings["baudrate"] == BAUD_RATE
    assert port.settings["bytesize"] == serial.EIGHTBITS
    assert port.settings["parity"] == serial.PARITY_NONE
    assert port.settings["stopbits"] == serial.STOPBITS_ONE
    assert port.settings["rtscts"] is False
    assert port.settings["dsrdtr"] is False
    assert port.settings["xonxoff"] is False
    assert port.settings["timeout"] == transport_module.READ_SLICE_SECONDS


def test_open_is_idempotent(port: StubPort) -> None:
    transport = SerialTransport(DEVICE)
    transport.open()
    first = port.settings
    transport.open()
    assert port.settings is first


def test_open_reports_a_missing_device(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(**_: object) -> StubPort:
        raise serial.SerialException("no such file or directory")

    monkeypatch.setattr(transport_module.serial, "Serial", factory)
    with pytest.raises(TransportError, match="cannot open"):
        SerialTransport(DEVICE).open()


def test_read_drains_the_buffered_burst(port: StubPort) -> None:
    transport = SerialTransport(DEVICE)
    transport.open()
    port.buffer += b"\xd0\xf9\xee\x5d"
    assert transport.read() == b"\xd0\xf9\xee\x5d"


def test_read_is_bounded_by_the_requested_size(port: StubPort) -> None:
    transport = SerialTransport(DEVICE)
    transport.open()
    port.buffer += b"abcdef"
    assert transport.read(2) == b"ab"


def test_an_idle_read_blocks_for_a_single_byte(port: StubPort) -> None:
    transport = SerialTransport(DEVICE)
    transport.open()
    assert transport.read() == b""


def test_a_failing_read_is_reported(port: StubPort) -> None:
    transport = SerialTransport(DEVICE)
    transport.open()
    port.fail_on.add("read")
    port.buffer += b"x"
    with pytest.raises(TransportError, match="read from"):
        transport.read()


def test_write_flushes_the_port(port: StubPort) -> None:
    transport = SerialTransport(DEVICE)
    transport.open()
    transport.write(b"hello")
    assert port.written == b"hello"
    assert port.flushes == 1


def test_a_failing_write_is_reported(port: StubPort) -> None:
    transport = SerialTransport(DEVICE)
    transport.open()
    port.fail_on.add("write")
    with pytest.raises(TransportError, match="write to"):
        transport.write(b"hello")


def test_using_a_closed_transport_is_an_error(port: StubPort) -> None:
    transport = SerialTransport(DEVICE)
    with pytest.raises(TransportError, match="is not open"):
        transport.read()
    with pytest.raises(TransportError, match="is not open"):
        transport.write(b"hello")


def test_close_releases_the_port_once(port: StubPort) -> None:
    transport = SerialTransport(DEVICE)
    transport.open()
    transport.close()
    transport.close()
    assert port.closed


def test_close_ignores_a_device_that_already_vanished(port: StubPort) -> None:
    transport = SerialTransport(DEVICE)
    transport.open()
    port.fail_on.add("close")
    transport.close()
    assert not port.closed
