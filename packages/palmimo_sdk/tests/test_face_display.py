"""FaceDisplay contract: command framing, replies, and EVT POWER dispatch.

Uses an in-memory fake serial (write/readline/close) injected via
``serial_factory``, so the host↔face-display contract is verified without hardware
or ``pyserial`` installed.
"""

import json
import queue
import threading
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from palmimo_sdk import FaceDisplay, FaceDisplayConnectTimeoutError, FaceDisplayError
from palmimo_sdk.io.display import FaceDisplayFirmwareError


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
        self.auto: Callable[[str], str | None] | None = lambda cmd: (
            "OK HELLO proto=2 fw=2.0 caps=assets" if cmd == "HELLO" else None
        )  # callable(cmd_str) -> reply_str | None
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


@pytest.mark.parametrize("hold_ms", [0, 3000])
@pytest.mark.parametrize(
    "name,asset",
    [
        ("Happy", "happy"),
        ("Happy ", "happy"),
        ("SMILE", "happy"),
        ("LAUGH", "happy"),
        ("SURPRISED", "surprise"),
        ("SLEEP", "sleeping"),
        ("LOVE", "love"),
    ],
)
def test_set_expression_is_deprecated_play_alias(hold_ms: int, name: str, asset: str) -> None:
    """Without this, supported legacy expressions send asset IDs that do not exist."""
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:
        fakes[0].auto = lambda cmd: f"OK PLAY palmimo/{asset}"
        with pytest.warns(DeprecationWarning):
            assert face.set_expression(name, hold_ms) == f"OK PLAY palmimo/{asset}"
    assert fakes[0].writes[-1] == (f"PLAY palmimo/{asset}" + (f" {hold_ms}" if hold_ms else "") + "\n").encode()


@pytest.mark.parametrize(
    ("method", "args", "command", "reply", "expected"),
    [
        ("wake", (), "WAKE", "OK WAKE", "OK WAKE"),
        ("power", (False,), "POWER OFF", "OK POWER OFF", "OK POWER OFF"),
        ("set_brightness", (50,), "BL 50", "OK BL 50", "OK BL 50"),
        ("stop", (), "STOP", "OK STOP", "OK STOP"),
        ("idle", (), "STOP", "OK STOP", "OK STOP"),
        ("ping", (), "PING", "PONG", True),
        ("version", (), "VERSION", "VERSION 2.0.0", "2.0.0"),
        ("stat", (), "STAT", "STAT power=ON proto=2", {"power": "ON", "proto": "2"}),
    ],
)
def test_commands_frame_and_parse_replies(
    method: str, args: tuple[Any, ...], command: str, reply: str, expected: Any
) -> None:
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:
        fakes[0].auto = lambda cmd: reply
        assert getattr(face, method)(*args) == expected
    assert fakes[0].writes == [b"HELLO\n", (command + "\n").encode()]


@pytest.mark.parametrize(
    "reply", ["ERR HELLO (try LIST)", "OK HELLO proto=1 fw=old caps=-", "OK HELLO proto=3 fw=new caps=-"]
)
def test_connect_rejects_incompatible_firmware(reply: str) -> None:
    fake = FakeSerial("fake", 115200)
    fake.auto = lambda cmd: reply
    face = FaceDisplay(port="fake", serial_factory=lambda *args, **kwargs: fake)
    with pytest.raises(FaceDisplayFirmwareError):
        face.connect()
    assert fake.closed and not face.is_connected


@pytest.mark.parametrize("reply", ["garbage\n", "OK STOP\n", "PONG\r\n", "PONG", "X" * 1025 + "\n", "PONG \xff\n"])
def test_invalid_reply_or_partial_line_cannot_satisfy_command(reply: str) -> None:
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", timeout=0.08, serial_factory=_factory(fakes)) as face:
        fakes[0].auto = lambda cmd: None
        fakes[0]._rx.put(reply.encode("latin1"))
        with pytest.raises(FaceDisplayError):
            face.ping()
        assert not face.is_connected


def test_partial_reads_are_reassembled() -> None:
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:
        fakes[0].auto = lambda cmd: None
        for part in [b"PO", b"NG", b"\n"]:
            fakes[0]._rx.put(part)
        assert face.ping()


def test_command_timeout_disconnects_before_late_reply_can_be_reused() -> None:
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", timeout=0.08, serial_factory=_factory(fakes)) as face:
        with pytest.raises(FaceDisplayError):
            face.ping()
        fakes[0].queue("PONG")
        with pytest.raises(FaceDisplayError):
            face.ping()
        assert fakes[0].closed


def test_events_do_not_steal_reply_and_callback_errors_do_not_kill_reader() -> None:
    fakes: list[FakeSerial] = []
    seen: list[tuple[int, int]] = []
    done = threading.Event()

    def tap(x: int, y: int) -> None:
        seen.append((x, y))
        done.set()
        raise ValueError("app callback failed")

    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:
        face.on_tap(tap)
        fakes[0].auto = lambda cmd: "EVT TOUCH REINIT\nEVT TAP 10 20\nPONG"
        assert face.ping()
        assert done.wait(1)
        assert face.ping()
    assert seen == [(10, 20), (10, 20)]


@pytest.mark.parametrize("method", ["ping", "connect"])
def test_callback_command_fails_without_deadlock(method: str) -> None:
    fakes: list[FakeSerial] = []
    rejected = threading.Event()
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:

        def tap(x: int, y: int) -> None:
            try:
                getattr(face, method)()
            except FaceDisplayError:
                rejected.set()

        face.on_tap(tap)
        fakes[0].auto = lambda command: "EVT TAP 1 2\nPONG"
        assert face.ping()
        assert rejected.wait(1)


def test_assets_follows_all_pages() -> None:
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:
        fakes[0].auto = lambda cmd: {
            "ASSET LIST": "OK ASSET LIST next=1 palmimo/happy:vector:20:1234abcd",
            "ASSET LIST 1": "OK ASSET LIST next=- zip.app/welcome:image:30:abcdef12",
        }[cmd]
        assert [a["id"] for a in face.assets()] == ["palmimo/happy", "zip.app/welcome"]


def test_asset_info_follows_all_slot_pages() -> None:
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:
        fakes[0].auto = lambda cmd: (
            "OK ASSET INFO palmimo/happy kind=vector bytes=20 crc32=1234abcd "
            + ("next=1 params=color/4" if cmd == "ASSET INFO palmimo/happy" else "next=- params=size/1")
        )
        info = face.asset_info("palmimo/happy")
        assert info == {
            "id": "palmimo/happy",
            "kind": "vector",
            "bytes": 20,
            "crc32": "1234abcd",
            "params": {"color": 4, "size": 1},
        }


@pytest.mark.parametrize("reply", ["ERR PARAM NOSLOT", "OK PARAM color"])
def test_param_missing_slot_returns_warning_reply(reply: str) -> None:
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:
        fakes[0].auto = lambda cmd: reply
        assert face.param("color", 1, 0, 0, 1) == reply
        assert fakes[0].writes[-1] == b"PARAM color 1 0 0 1\n"


@pytest.mark.parametrize("reply", ["ERR PLAY UNKNOWN", "ERR PLAY POWEROFF", "ERR PLAY BUSY"])
def test_firmware_error_is_raised_without_desynchronizing(reply: str) -> None:
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:
        fakes[0].auto = lambda cmd: reply if cmd.startswith("PLAY") else "PONG"
        with pytest.raises(FaceDisplayError):
            face.play("palmimo/happy")
        assert face.ping()


@pytest.mark.parametrize("final", ["OK STREAM", "ERR STREAM CRC", "ERR XFER TIMEOUT"])
def test_stream_obeys_window_and_consumes_ack_and_final(final: str) -> None:
    fake = FakeSerial("fake", 115200)
    payload = bytes(range(256)) * 200
    received = bytearray()
    acknowledged = 0
    max_ahead = 0
    original_read = fake.readline

    def read() -> bytes:
        nonlocal acknowledged
        line = original_read()
        if line.startswith(b"ACK "):
            acknowledged = int(line.split()[1])
        return line

    fake.readline = read  # type: ignore[method-assign]
    original_write = fake.write

    def write(data: bytes) -> int:
        nonlocal max_ahead
        if not received and data.startswith(b"STREAM "):
            assert data == f"STREAM 160 160 RGB565 {zlib.crc32(payload):08x}\n".encode()
            fake.queue(f"OK XFER BEGIN {len(payload)} ACK=4096")
        elif data in (b"HELLO\n", b"PING\n"):
            return original_write(data)
        else:
            received.extend(data)
            max_ahead = max(max_ahead, len(received) - acknowledged)
            start = len(received) - len(data)
            for offset in range((start // 4096 + 1) * 4096, len(received) + 1, 4096):
                fake.queue(f"ACK {offset}")
            if len(received) == len(payload):
                if len(payload) % 4096:
                    fake.queue(f"ACK {len(payload)}")
                fake.queue(final)
        return len(data)

    fake.write = write  # type: ignore[method-assign]
    with FaceDisplay(port="fake", serial_factory=lambda *args, **kwargs: fake) as face:
        if final == "OK STREAM":
            assert face.stream_frame(160, 160, payload) == final
            fake.auto = lambda cmd: "PONG"
            assert face.ping()
        else:
            with pytest.raises(FaceDisplayError):
                face.stream_frame(160, 160, payload)
            assert face.is_connected == (final != "ERR XFER TIMEOUT")
    assert received == payload
    assert max_ahead <= 16384


@pytest.mark.parametrize("locale", [None, "ja-jp", "ja", "fr"])
def test_catalog_joins_owned_declared_present_assets_and_localizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locale: str | None
) -> None:
    monkeypatch.setenv("PALMIMO_APP_ID", "zip.app")
    standard = tmp_path / "catalog.json"
    app_dir = tmp_path / "apps" / "zip.app"
    app_dir.mkdir(parents=True)

    def declaration(id: str) -> dict[str, Any]:
        return {
            "id": id,
            "description": "Smile",
            "tags": ["face"],
            "params": {"color": {"description": "Colour"}, "removed": {"description": "Gone"}},
            "translations": {"ja": {"description": "笑顔", "params.color": "色"}, "ja-jp": {"description": "笑み"}},
        }

    standard.write_text(
        json.dumps(
            {
                "schema": 1,
                "assets": [
                    declaration("palmimo/happy"),
                    declaration("palmimo/absent"),
                    declaration("zip.other/stolen"),
                ],
            }
        )
    )
    (app_dir / "assets.json").write_text(json.dumps({"schema": 1, "assets": [declaration("zip.app/welcome")]}))
    monkeypatch.setattr("palmimo_sdk.io.display.STANDARD_CATALOG_PATH", standard)
    monkeypatch.setattr("palmimo_sdk.io.display.APP_ASSETS_ROOT", tmp_path / "apps")
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:

        def respond(cmd: str) -> str:
            if cmd == "ASSET LIST":
                return "OK ASSET LIST next=- palmimo/happy:image:20:1234abcd zip.app/welcome:vector:20:1234abcd zip.other/stolen:vector:20:1234abcd"
            if cmd.startswith("ASSET INFO"):
                id = cmd.split()[2]
                return f"OK ASSET INFO {id} kind={'image' if id.startswith('palmimo/') else 'vector'} bytes=20 crc32=1234abcd next=- params={'-' if id.startswith('palmimo/') else 'color/4'}"
            return "OK PLAY " + cmd.split()[1]

        fakes[0].auto = respond
        catalog = face.catalog(tags=["face"], locale=locale)
        assert [a["id"] for a in catalog] == ["palmimo/happy", "zip.app/welcome"]
        assert catalog[0]["kind"] == "image" and catalog[0]["params"] == {}
        assert catalog[1]["params"]["color"] == {
            "description": "色" if locale in ("ja", "ja-jp") else "Colour",
            "dim": 4,
        }
        assert catalog[0]["description"] == {None: "Smile", "ja": "笑顔", "ja-jp": "笑み", "fr": "Smile"}[locale]
        assert catalog[0]["translations"]["ja"]["description"] == "笑顔"
        assert face.catalog(tags=["other"]) == []
        assert face.play("zip.app/welcome") == "OK PLAY zip.app/welcome"
        with pytest.raises(ValueError):
            face.play("zip.other/stolen")
        assert face.play("palmimo/unlisted") == "OK PLAY palmimo/unlisted"


@pytest.mark.parametrize("id", ["palmimo/happy\nSTOP", "palmimo/happy\r", "é/foo", "../foo", "palmimo/", "zip.other/x"])
def test_play_rejects_invalid_or_unowned_ids_before_writing(id: str) -> None:
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:
        with pytest.raises(ValueError):
            face.play(id)
        assert fakes[0].writes == [b"HELLO\n"]


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


def test_reconnect_while_old_command_times_out_cannot_deadlock() -> None:
    fakes: list[FakeSerial] = []
    face = FaceDisplay(port="fake", timeout=0.1, serial_factory=_factory(fakes))
    face.connect()
    started = threading.Event()
    done = threading.Event()
    reconnected = threading.Event()

    def no_reply(cmd: str) -> None:
        started.set()

    fakes[0].auto = no_reply

    def command() -> None:
        try:
            face.ping()
        except FaceDisplayError:
            pass
        finally:
            done.set()

    def reconnect() -> None:
        face.connect()
        reconnected.set()

    threading.Thread(target=command, daemon=True).start()
    assert started.wait(1)
    face.disconnect()
    threading.Thread(target=reconnect, daemon=True).start()
    assert done.wait(1) and reconnected.wait(1)
    fakes[-1].auto = lambda cmd: "PONG"
    assert face.ping()
    face.disconnect()


@pytest.mark.parametrize("reply", ["OK ASSET LIST next=0", "OK ASSET LIST next=oops", "OK ASSET LIST next=- broken"])
def test_asset_listing_rejects_invalid_pages(reply: str) -> None:
    """Without this, malformed page structure can corrupt catalog pagination."""
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:
        fakes[0].auto = lambda cmd: reply
        with pytest.raises(FaceDisplayError):
            face.assets()
        assert not face.is_connected


@pytest.mark.parametrize("reply", ["next=0 params=color/4"])
def test_asset_info_rejects_invalid_slot_pages(reply: str) -> None:
    """Without this, a nonadvancing slot cursor can loop indefinitely."""
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:
        fakes[0].auto = lambda cmd: "OK ASSET INFO palmimo/a kind=vector bytes=1 crc32=1234abcd " + reply
        with pytest.raises(FaceDisplayError):
            face.asset_info("palmimo/a")
        assert not face.is_connected


@pytest.mark.parametrize("progress", ["ACK 0", "ACK 3", "OK STREAM", "OK XFER BEGIN 2 ACK=2048"])
def test_stream_rejects_invalid_progress_and_disconnects(progress: str) -> None:
    fake = FakeSerial("fake", 115200)
    original_write = fake.write

    def write(data: bytes) -> int:
        if data == b"HELLO\n":
            return original_write(data)
        if data.startswith(b"STREAM "):
            fake.queue(progress if progress.startswith("OK XFER") else "OK XFER BEGIN 2 ACK=4096")
        else:
            fake.queue(progress)
        return len(data)

    fake.write = write  # type: ignore[method-assign]
    with FaceDisplay(port="fake", serial_factory=lambda *args, **kwargs: fake) as face:
        with pytest.raises(FaceDisplayError):
            face.stream_frame(1, 1, b"\0\0")
        assert not face.is_connected


def test_short_serial_writes_are_completed() -> None:
    fake = FakeSerial("fake", 115200)
    received = bytearray()

    def write(data: bytes) -> int:
        received.extend(data[:2])
        if received.endswith(b"\n"):
            command = received.decode().strip()
            received.clear()
            fake.queue("OK HELLO proto=2 fw=2.0 caps=assets" if command == "HELLO" else "PONG")
        return min(2, len(data))

    fake.write = write  # type: ignore[method-assign]
    with FaceDisplay(port="fake", serial_factory=lambda *args, **kwargs: fake) as face:
        assert face.ping()


@pytest.mark.parametrize("w,h,payload", [(0, 1, b""), (170, 1, b""), (1, 1, b"\0")])
def test_stream_rejects_invalid_frames_before_sending(w: int, h: int, payload: bytes) -> None:
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:
        with pytest.raises(ValueError):
            face.stream_frame(w, h, payload)
        assert fakes[0].writes == [b"HELLO\n"]


@pytest.mark.parametrize("stage", ["body", "reply"])
def test_interrupted_transfer_disconnects(stage: str) -> None:
    fake = FakeSerial("fake", 115200)
    original_write = fake.write

    def write(data: bytes) -> int:
        if data == b"HELLO\n":
            return original_write(data)
        if data.startswith(b"STREAM "):
            fake.queue("OK XFER BEGIN 2 ACK=4096")
        elif stage == "body":
            raise KeyboardInterrupt
        else:
            fake.raise_on_read = OSError("disconnected during transfer")
        return len(data)

    fake.write = write  # type: ignore[method-assign]
    face = FaceDisplay(port="fake", timeout=0.1, serial_factory=lambda *args, **kwargs: fake)
    face.connect()
    with pytest.raises(KeyboardInterrupt if stage == "body" else FaceDisplayError):
        face.stream_frame(1, 1, b"\0\0")
    assert not face.is_connected and fake.closed


def test_serial_configuration_failure_closes_opened_port() -> None:
    class ConfigurationFailure(FakeSerial):
        @property
        def write_timeout(self) -> float:
            return 0.1

        @write_timeout.setter
        def write_timeout(self, value: float) -> None:
            raise OSError("configuration failed")

    fake = ConfigurationFailure("fake", 115200)
    face = FaceDisplay(port="fake", serial_factory=lambda *args, **kwargs: fake)
    with pytest.raises(FaceDisplayError):
        face.connect()
    assert fake.closed


@pytest.mark.parametrize("value", [1e-12, 1e100, -0.1])
def test_param_preserves_numeric_values(value: float) -> None:
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:

        def respond(command: str) -> str:
            assert float(command.split()[2]) == value
            return "OK PARAM size"

        fakes[0].auto = respond
        assert face.param("size", value) == "OK PARAM size"


@pytest.mark.parametrize("app_id", [None, "../escape", "Acme.my-app"])
def test_catalog_ignores_unowned_assets_and_invalid_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_id: str | None
) -> None:
    standard = tmp_path / "catalog.json"
    standard.write_text(
        json.dumps(
            {
                "schema": 1,
                "assets": [
                    None,
                    {"id": "palmimo/no-description"},
                    {"id": "other.app/x", "description": "Unowned"},
                    {"id": "palmimo/a", "description": "Standard"},
                ],
            }
        )
    )
    monkeypatch.setattr("palmimo_sdk.io.display.STANDARD_CATALOG_PATH", standard)
    monkeypatch.setattr("palmimo_sdk.io.display.APP_ASSETS_ROOT", tmp_path)
    if app_id is None:
        monkeypatch.delenv("PALMIMO_APP_ID", raising=False)
    else:
        monkeypatch.setenv("PALMIMO_APP_ID", app_id)
    if app_id == "Acme.my-app":
        app_dir = tmp_path / app_id
        app_dir.mkdir()
        (app_dir / "assets.json").write_text(
            json.dumps({"schema": 1, "assets": [{"id": app_id + "/x", "description": "App"}]})
        )
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:

        def respond(command: str) -> str:
            if command == "ASSET LIST":
                return "OK ASSET LIST next=- palmimo/a:image:1:1234abcd Acme.my-app/x:image:1:1234abcd other.app/x:image:1:1234abcd"
            return f"OK ASSET INFO {command.split()[2]} kind=image bytes=1 crc32=1234abcd next=- params=-"

        fakes[0].auto = respond
        expected = ["palmimo/a"] + (["Acme.my-app/x"] if app_id == "Acme.my-app" else [])
        assert [a["id"] for a in face.catalog()] == expected


def test_catalog_cache_is_isolated_from_callers_and_refreshed_on_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "catalog.json"
    monkeypatch.setattr("palmimo_sdk.io.display.STANDARD_CATALOG_PATH", path)
    monkeypatch.delenv("PALMIMO_APP_ID", raising=False)

    def save(description: str) -> None:
        path.write_text(json.dumps({"schema": 1, "assets": [{"id": "palmimo/a", "description": description}]}))

    fakes: list[FakeSerial] = []

    def factory(port: str, baudrate: int, timeout: float) -> FakeSerial:
        fake = FakeSerial(port, baudrate, timeout)

        def respond(command: str) -> str:
            if command == "HELLO":
                return "OK HELLO proto=2 fw=2 caps=assets"
            if command == "ASSET LIST":
                return "OK ASSET LIST next=- palmimo/a:image:1:1234abcd"
            return "OK ASSET INFO palmimo/a kind=image bytes=1 crc32=1234abcd next=- params=-"

        fake.auto = respond
        fakes.append(fake)
        return fake

    save("First")
    face = FaceDisplay(port="fake", serial_factory=factory)
    with face:
        catalog = face.catalog()
        catalog[0]["description"] = "Caller mutation"
        save("Second")
        assert face.catalog()[0]["description"] == "First"
        assert fakes[0].writes.count(b"ASSET LIST\n") == 1
    with face:
        assert face.catalog()[0]["description"] == "Second"


@pytest.mark.parametrize("reply", ["OK HELLO proto=2 fw=2 caps=assets extra=1", "OK HELLO proto=2 fw=2 caps="])
def test_connect_accepts_extensible_hello_fields(reply: str) -> None:
    """Without this, firmware capability changes prevent an otherwise compatible connection."""
    fake = FakeSerial("fake", 115200)
    fake.auto = lambda cmd: reply if cmd == "HELLO" else "PONG"
    with FaceDisplay(port="fake", serial_factory=lambda *args, **kwargs: fake) as face:
        assert face.ping()


@pytest.mark.parametrize(
    "entry", ["palmimo/Happy:video:1:1234abcd", "palmimo/a:image:1:badcrc", "bad:image:1:1234abcd"]
)
def test_asset_listing_keeps_valid_entries_and_connection(entry: str, caplog: pytest.LogCaptureFixture) -> None:
    """Without this, one unsupported or invalid flash entry takes the entire display offline."""
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:
        fakes[0].auto = lambda cmd: "OK ASSET LIST next=- " + entry + " palmimo/happy:image:1:1234abcd"
        assets = face.assets()
        assert any(a["id"] == "palmimo/happy" for a in assets)
        if entry.startswith("palmimo/Happy"):
            assert assets[0]["id"] == "palmimo/Happy" and assets[0]["kind"] == "video"
        else:
            assert len(assets) == 1
            assert any(r.levelname == "WARNING" for r in caplog.records)
        fakes[0].auto = lambda cmd: "PONG"
        assert face.ping()


@pytest.mark.parametrize(
    "slots,expected",
    [
        ("color/145", {"color": 145}),
        ("color/0,good/4", {"good": 4}),
        ("BAD/4,good/4", {"good": 4}),
        ("color/4,color/4", {"color": 4}),
    ],
)
def test_asset_info_keeps_valid_slots_and_connection(slots: str, expected: dict[str, int]) -> None:
    """Without this, one bad slot or an undocumented dimension limit prevents startup."""
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:
        fakes[0].auto = lambda cmd: "OK ASSET INFO palmimo/a kind=video bytes=1 crc32=1234abcd next=- params=" + slots
        info = face.asset_info("palmimo/a")
        assert info is not None and info["params"] == expected
        fakes[0].auto = lambda cmd: "PONG"
        assert face.ping()


def test_catalog_skips_invalid_info_without_losing_other_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this, corrupt asset metadata prevents the companion from loading any catalog."""
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "assets": [
                    {"id": "palmimo/happy", "description": "Happy"},
                    {"id": "palmimo/sad", "description": "Sad"},
                ],
            }
        )
    )
    monkeypatch.setattr("palmimo_sdk.io.display.STANDARD_CATALOG_PATH", path)
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:

        def reply(cmd: str) -> str:
            if cmd == "ASSET LIST":
                return "OK ASSET LIST next=- palmimo/happy:image:1:1234abcd palmimo/sad:image:1:1234abcd"
            id = cmd.split()[2]
            crc = "badcrc" if id == "palmimo/sad" else "1234abcd"
            return f"OK ASSET INFO {id} kind=image bytes=1 crc32={crc} next=- params=-"

        fakes[0].auto = reply
        assert [a["id"] for a in face.catalog()] == ["palmimo/happy"]
        assert face.is_connected


@pytest.mark.parametrize(
    "name,hold_ms,error",
    [
        ("hatena", 0, FaceDisplayError),
        ("bikkuri", 0, FaceDisplayError),
        ("wakeup", 0, FaceDisplayError),
        ("curry", 0, FaceDisplayError),
        ("IDEA", 0, FaceDisplayError),
        ("HEART", 0, FaceDisplayError),
        ("../happy", 0, ValueError),
        ("😀", 0, ValueError),
        ("Happy", -1, ValueError),
    ],
)
def test_set_expression_rejects_unsupported_names_without_play(name: str, hold_ms: int, error: type[Exception]) -> None:
    """Without this, removed aliases can unexpectedly play unrelated assets or return ERR strings."""
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:
        with pytest.warns(DeprecationWarning), pytest.raises(error):
            face.set_expression(name, hold_ms)
        assert fakes[0].writes == [b"HELLO\n"]


def test_set_expression_raises_on_firmware_rejection() -> None:
    """Without this, callers cannot rely on exceptions for failed deprecated playback."""
    fakes: list[FakeSerial] = []
    with FaceDisplay(port="fake", serial_factory=_factory(fakes)) as face:
        fakes[0].auto = lambda cmd: "ERR PLAY UNKNOWN"
        with pytest.warns(DeprecationWarning), pytest.raises(FaceDisplayError):
            face.set_expression("HAPPY")
        assert face.is_connected
