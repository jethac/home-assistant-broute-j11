"""Tests for the blocking pyserial transport.

pyserial is replaced by a stub port so the wire settings, the bounded read
slice and the error translation are all exercised without hardware.
"""

from __future__ import annotations

import traceback

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


def rendered(err: BaseException) -> str:
    """Return ``err`` as Home Assistant would log it, causes included.

    A chained pyserial error keeps naming the port in the traceback even when
    the message does not, so the whole rendering has to stay path-free
    (PRD §6.7).
    """
    return "".join(traceback.format_exception(err))


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
        raise serial.SerialException(f"could not open port {DEVICE}")

    monkeypatch.setattr(transport_module.serial, "Serial", factory)
    with pytest.raises(TransportError, match="could not be opened") as raised:
        SerialTransport(DEVICE).open()
    # The path embeds the adapter's USB serial number (PRD §6.7).
    assert DEVICE not in rendered(raised.value)


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


def test_an_os_error_keeps_its_reason_without_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def factory(**_: object) -> StubPort:
        raise FileNotFoundError(2, "No such file or directory", DEVICE)

    monkeypatch.setattr(transport_module.serial, "Serial", factory)
    with pytest.raises(TransportError) as raised:
        SerialTransport(DEVICE).open()
    assert "No such file or directory" in str(raised.value)
    assert DEVICE not in rendered(raised.value)


def test_a_pyserial_error_that_names_the_port_in_strerror_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def factory(**_: object) -> StubPort:
        # pyserial's POSIX backend passes its own formatted message, which
        # names the port, as the OSError strerror.
        raise serial.SerialException(2, f"could not open port {DEVICE}")

    monkeypatch.setattr(transport_module.serial, "Serial", factory)
    with pytest.raises(TransportError) as raised:
        SerialTransport(DEVICE).open()
    assert DEVICE not in rendered(raised.value)


def test_a_failing_read_is_reported(port: StubPort) -> None:
    transport = SerialTransport(DEVICE)
    transport.open()
    port.fail_on.add("read")
    port.buffer += b"x"
    with pytest.raises(TransportError, match="reading from") as raised:
        transport.read()
    assert DEVICE not in rendered(raised.value)


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
    with pytest.raises(TransportError, match="writing to") as raised:
        transport.write(b"hello")
    assert DEVICE not in rendered(raised.value)


def test_using_a_closed_transport_is_an_error(port: StubPort) -> None:
    transport = SerialTransport(DEVICE)
    with pytest.raises(TransportError, match="is not open") as read_error:
        transport.read()
    with pytest.raises(TransportError, match="is not open") as write_error:
        transport.write(b"hello")
    assert DEVICE not in rendered(read_error.value)
    assert DEVICE not in rendered(write_error.value)


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
