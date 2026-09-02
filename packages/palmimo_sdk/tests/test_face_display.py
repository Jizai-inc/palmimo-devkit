"""FaceDisplay contract: command framing, replies, and EVT POWER dispatch.

Uses an in-memory fake serial (write/readline/close) injected via
``serial_factory``, so the host↔face-display contract is verified without hardware
or ``pyserial`` installed.
"""

import queue
import threading
import time
from collections.abc import Callable

import pytest

from palmimo_sdk import FaceDisplay, FaceDisplayConnectTimeoutError, FaceDisplayError


class FakeSerial:
    """Thread-safe serial stand-in: records writes, replays queued reply lines.

    Reads come off a :class:`queue.Queue` so the background EVT reader thread and
    the command thread can use it concurrently (the real bug the reviewers
    flagged only shows up when a reader thread is live). An optional *auto*
    responder enqueues a reply *in response to* each write, mirroring how the
    firmware answers a command — so reader-active reply tests aren't racy.
    """

    def __init__(self, port: str, baudrate: int, timeout: float = 1.0) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.writes: list[bytes] = []
        self._rx: queue.Queue[bytes] = queue.Queue()
        self.auto: Callable[[str], str | None] | None = None  # callable(cmd_str) -> reply_str | None
        self.raise_on_read: BaseException | None = None  # set to an exception to simulate a USB drop
        self.closed = False

    # -- host → device
    def write(self, data: bytes) -> int:
        self.writes.append(data)
        if self.auto is not None:
            reply = self.auto(data.decode().strip())
            if reply is not None:
                self._rx.put((reply + "\n").encode())
        return len(data)

    # -- device → host
    def queue(self, *lines: str) -> None:
        for ln in lines:
            self._rx.put((ln + "\n").encode())

    def readline(self) -> bytes:
        if self.raise_on_read is not None:
            raise self.raise_on_read  # simulate USB unplug / device reset
        try:
            return self._rx.get(timeout=0.05)  # mimic a read timeout when idle
        except queue.Empty:
            return b""

    def close(self) -> None:
        self.closed = True


def _factory(captured: list[FakeSerial]) -> Callable[..., FakeSerial]:
    """serial_factory that records the built FakeSerial into *captured*."""

    def make(port: str, baudrate: int, timeout: float = 1.0) -> FakeSerial:
        fake = FakeSerial(port, baudrate, timeout)
        captured.append(fake)
        return fake

    return make


def test_set_expression_frames_command_and_returns_reply() -> None:
    """Sends EXPR <NAME> and returns the firmware's OK reply as-is."""
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="COM_TEST", serial_factory=_factory(fakes)) as face:
        fakes[0].queue("OK HAPPY")
        reply = face.set_expression("happy")
    assert fakes[0].writes == [b"EXPR HAPPY\n"]
    assert reply == "OK HAPPY"


def test_set_expression_with_hold_appends_ms() -> None:
    """When hold_ms>0, the format is EXPR <NAME> <ms> (auto-returns to IDLE)."""
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="COM_TEST", serial_factory=_factory(fakes)) as face:
        fakes[0].queue("OK HAPPY")
        face.set_expression("Happy", hold_ms=3000)
    assert fakes[0].writes == [b"EXPR HAPPY 3000\n"]


def test_power_and_wake_and_brightness_framing() -> None:
    """POWER/WAKE/BL are framed as a single protocol-compliant line."""
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="COM_TEST", serial_factory=_factory(fakes)) as face:
        fakes[0].queue("OK WAKE", "OK POWER OFF", "OK BL 50")
        face.wake()
        face.power(False)
        face.set_brightness(50)
    assert fakes[0].writes == [b"WAKE\n", b"POWER OFF\n", b"BL 50\n"]


def test_ping_true_on_pong() -> None:
    """PING -> PONG returns True."""
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="COM_TEST", serial_factory=_factory(fakes)) as face:
        fakes[0].queue("PONG")
        assert face.ping() is True


def test_command_before_connect_raises() -> None:
    """A command before connect raises FaceDisplayError."""
    face = FaceDisplay(port="COM_TEST", serial_factory=_factory([]))
    with pytest.raises(FaceDisplayError, match="Not connected"):
        face.idle()


def test_context_manager_closes_port() -> None:
    """Exiting the with block closes the serial connection."""
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="COM_TEST", serial_factory=_factory(fakes)) as face:
        assert face.is_connected
    assert fakes[0].closed is True
    assert not face.is_connected


def test_commands_return_replies_while_reader_active() -> None:
    """Command replies still reach the caller even with on_power_event set (the reader doesn't steal them)."""
    fakes: list[FakeSerial] = []
    replies = {"PING": "PONG", "EXPR HAPPY": "OK HAPPY"}

    face = FaceDisplay(port="COM_TEST", on_power_event=lambda on: None, serial_factory=_factory(fakes))
    face.connect()
    # Auto-responder answers each command like the firmware does — so the live
    # reader thread routes the reply back through the queue to the caller.
    fakes[0].auto = lambda cmd: replies.get(cmd)
    try:
        assert face.ping() is True
        assert face.set_expression("happy") == "OK HAPPY"
    finally:
        face.disconnect()


def test_command_reply_times_out_to_empty_with_reader_active() -> None:
    """If no reply arrives while the reader is active, it returns "" (ping is False, no exception is raised)."""
    fakes: list[FakeSerial] = []
    face = FaceDisplay(port="COM_TEST", timeout=0.2, on_power_event=lambda on: None, serial_factory=_factory(fakes))
    face.connect()  # no auto-responder → no reply ever arrives
    try:
        assert face.ping() is False
    finally:
        face.disconnect()


def test_evt_power_dispatches_to_callback() -> None:
    """The receiver thread delivers EVT POWER ON|OFF to the callback."""
    fakes: list[FakeSerial] = []
    seen: list[bool] = []
    done = threading.Event()

    def on_power(power_on: bool) -> None:
        seen.append(power_on)
        done.set()

    with FaceDisplay(port="COM_TEST", on_power_event=on_power, serial_factory=_factory(fakes)):
        fakes[0].queue("EVT POWER ON")
        assert done.wait(timeout=2.0), "callback was not invoked"
    assert seen == [True]


def test_read_failure_marks_disconnected() -> None:
    """If readline raises (e.g. a USB unplug), is_connected becomes False (leaving the reconnect decision to the caller)."""
    fakes: list[FakeSerial] = []
    face = FaceDisplay(port="COM_TEST", on_power_event=lambda on: None, serial_factory=_factory(fakes))
    face.connect()
    assert face.is_connected
    # Simulate the cable being yanked: the reader's next readline raises.
    fakes[0].raise_on_read = OSError("usb yanked")
    deadline = time.time() + 2.0
    while face.is_connected and time.time() < deadline:
        time.sleep(0.02)
    assert not face.is_connected, "read failure should tear the connection down"
    face.disconnect()  # idempotent / safe after the reader already died


# ----------------------------------------------------------------------
# connect() fail-fast timeout: on a dev machine with no face
# display attached, or the wrong device on the matched port, opening the
# serial port can block indefinitely. A serial_factory that never returns
# stands in for that; connect() must give up after connect_timeout instead of
# hanging, naming the port in a clear, actionable error.
# ----------------------------------------------------------------------


def test_connect_times_out_when_port_never_opens() -> None:
    """A serial_factory that never returns makes connect() raise FaceDisplayConnectTimeoutError
    within roughly connect_timeout, instead of hanging forever."""
    never_return = threading.Event()  # never set -> factory blocks "forever"

    def hanging_factory(port: str, baudrate: int, timeout: float = 1.0) -> FakeSerial:
        never_return.wait()  # simulates an unresponsive serial open
        return FakeSerial(port, baudrate, timeout)  # pragma: no cover - unreachable in the test

    face = FaceDisplay(port="COM_TEST", serial_factory=hanging_factory, connect_timeout=0.05)

    start = time.perf_counter()
    with pytest.raises(FaceDisplayConnectTimeoutError, match="COM_TEST"):
        face.connect()
    elapsed = time.perf_counter() - start

    assert elapsed < 2.0, "connect() should fail fast, not hang"
    assert not face.is_connected


def test_connect_timeout_error_names_device_and_port() -> None:
    """The timeout error names the face display and the port it was probing."""
    never_return = threading.Event()

    def hanging_factory(port: str, baudrate: int, timeout: float = 1.0) -> FakeSerial:
        never_return.wait()
        return FakeSerial(port, baudrate, timeout)  # pragma: no cover

    face = FaceDisplay(port="COM_HANG", serial_factory=hanging_factory, connect_timeout=0.05)
    with pytest.raises(FaceDisplayConnectTimeoutError, match="face display") as exc_info:
        face.connect()
    assert "COM_HANG" in str(exc_info.value)


def test_connect_timeout_is_a_face_display_error() -> None:
    """FaceDisplayConnectTimeoutError is a FaceDisplayError, so existing callers that
    catch the broad type still catch the timeout too."""
    never_return = threading.Event()

    def hanging_factory(port: str, baudrate: int, timeout: float = 1.0) -> FakeSerial:
        never_return.wait()
        return FakeSerial(port, baudrate, timeout)  # pragma: no cover

    face = FaceDisplay(port="COM_TEST", serial_factory=hanging_factory, connect_timeout=0.05)
    with pytest.raises(FaceDisplayError):
        face.connect()


def test_connect_within_timeout_succeeds_normally() -> None:
    """A serial_factory that returns promptly is unaffected by the connect_timeout guard."""
    fakes: list[FakeSerial] = []
    face = FaceDisplay(port="COM_TEST", serial_factory=_factory(fakes), connect_timeout=0.05)
    face.connect()
    assert face.is_connected
    face.disconnect()


def test_connect_timeout_error_names_auto_detected_port_when_resolved_before_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """port=None: if auto-detection resolves a port before the serial open itself hangs,
    the timeout error names that resolved port instead of a generic placeholder."""
    monkeypatch.setattr("palmimo_sdk.io.display.find_face_port", lambda: "COM_DETECTED9")
    never_return = threading.Event()

    def hanging_factory(port: str, baudrate: int, timeout: float = 1.0) -> FakeSerial:
        never_return.wait()
        return FakeSerial(port, baudrate, timeout)  # pragma: no cover

    face = FaceDisplay(serial_factory=hanging_factory, connect_timeout=0.05)
    with pytest.raises(FaceDisplayConnectTimeoutError, match="COM_DETECTED9"):
        face.connect()


def test_connect_timeout_closes_late_arriving_port_once_worker_unblocks() -> None:
    """Late-arriving port: the serial open can eventually finish successfully after
    connect() already gave up and raised FaceDisplayConnectTimeoutError. That serial
    object is orphaned -- self._ser was never assigned, so it's invisible to any
    rollback -- and must be closed rather than leaked open to race a caller's retry
    for the same port."""
    release = threading.Event()
    fakes: list[FakeSerial] = []

    def hanging_factory(port: str, baudrate: int, timeout: float = 1.0) -> FakeSerial:
        release.wait()  # blocks past the deadline until the test unblocks it
        fake = FakeSerial(port, baudrate, timeout)
        fakes.append(fake)
        return fake

    face = FaceDisplay(port="COM_TEST", serial_factory=hanging_factory, connect_timeout=0.05)

    with pytest.raises(FaceDisplayConnectTimeoutError):
        face.connect()
    assert fakes == []  # not yet -- the worker is still blocked
    assert not face.is_connected  # the failed connect owns nothing

    release.set()  # let the abandoned worker finish, opening the port late
    deadline = time.time() + 2.0
    while not fakes and time.time() < deadline:
        time.sleep(0.01)

    assert len(fakes) == 1
    late_deadline = time.time() + 2.0
    while not fakes[0].closed and time.time() < late_deadline:
        time.sleep(0.01)
    assert fakes[0].closed, "the orphaned, late-arriving serial port should be closed"
    assert not face.is_connected  # the late cleanup never assigns self._ser
