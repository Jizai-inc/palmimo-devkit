"""HeadCamera resource tests.

Only the hardware bit (``cv2.VideoCapture``) is faked — the full-FOV capture is
replaced with a synthetic frame, while the real ``cv2.resize`` / ``cv2.rotate``
run so the downscale-and-rotate behaviour is genuinely exercised.

Covers the one-shot contract (open / read / close) and the background drain:
fan-out to ``add_consumer``, ``latest()``, idempotent start/stop, the
read-failure backoff, and the close-vs-in-flight-read race.
"""

import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from palmimo_sdk.io import HeadCamera, HeadCameraConfig
from palmimo_sdk.io import camera as camera_module


cv2 = pytest.importorskip("cv2")


CAPTURE_SHAPE = (1944, 2592, 3)  # native full-FOV frame (h, w, c)


def _full_frame() -> Any:
    """A black full-sensor frame with a bright marker in the TOP-LEFT corner."""
    frame = np.zeros(CAPTURE_SHAPE, dtype=np.uint8)
    frame[0:200, 0:200] = 255
    return frame


class _FakeCap:
    def __init__(self, frame_factory: Callable[[], Any]) -> None:
        self._frame_factory = frame_factory
        self._opened = True
        self.fail = False

    def isOpened(self) -> bool:  # noqa: N802 — mirrors the cv2.VideoCapture API
        return self._opened

    def set(self, *_args: Any) -> bool:
        return True

    def read(self) -> tuple[bool, Any]:
        if self.fail:
            return False, None
        return True, self._frame_factory()

    def release(self) -> None:
        self._opened = False


class _CapList(list[_FakeCap]):
    """List of constructed fake caps, plus a hook to swap the source frame."""

    set_frame: Callable[[Any], None] | None = None  # assigned per-fixture


@pytest.fixture
def fake_capture(monkeypatch: pytest.MonkeyPatch) -> _CapList:
    """Patch cv2.VideoCapture; return the list of constructed fake caps."""
    created = _CapList()
    frame_factory = {"fn": _full_frame}

    def factory(_device: Any) -> _FakeCap:
        cap = _FakeCap(frame_factory["fn"])
        created.append(cap)
        return cap

    monkeypatch.setattr(cv2, "VideoCapture", factory)
    created.set_frame = lambda fn: frame_factory.__setitem__("fn", fn)
    return created


def test_config_defaults() -> None:
    c = HeadCameraConfig()
    assert (c.capture_width, c.capture_height) == (2592, 1944)
    assert (c.proc_width, c.proc_height) == (1296, 972)
    assert c.rotation == 180


def test_invalid_rotation_rejected() -> None:
    with pytest.raises(ValueError):
        HeadCameraConfig(rotation=45)


def test_read_downscales_to_processing_size(fake_capture: _CapList) -> None:
    cam = HeadCamera(HeadCameraConfig(rotation=0))
    ok, frame = cam.read()
    assert ok
    assert frame.shape == (972, 1296, 3)  # half of 1944x2592


def test_rotation_180_moves_marker_to_opposite_corner(fake_capture: _CapList) -> None:
    cam = HeadCamera(HeadCameraConfig(rotation=180))
    _, frame = cam.read()
    # marker started top-left; after 180 it must be bottom-right
    assert frame[-1, -1].max() == 255
    assert frame[0, 0].max() == 0


def test_no_rotation_keeps_marker_top_left(fake_capture: _CapList) -> None:
    cam = HeadCamera(HeadCameraConfig(rotation=0))
    _, frame = cam.read()
    assert frame[0, 0].max() == 255
    assert frame[-1, -1].max() == 0


def test_open_is_idempotent_across_reads(fake_capture: _CapList) -> None:
    cam = HeadCamera()
    cam.open()
    cam.read()
    cam.read()
    assert len(fake_capture) == 1  # device opened once, not per read


def test_read_returns_false_on_failed_grab(fake_capture: _CapList) -> None:
    cam = HeadCamera()
    cam.open()
    fake_capture[0].fail = True
    ok, frame = cam.read()
    assert ok is False
    assert frame is None


def test_read_returns_false_when_device_cannot_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device that fails to open is reported as (False, None), not raised."""

    def factory(_device: Any) -> _FakeCap:
        cap = _FakeCap(_full_frame)
        cap._opened = False  # cv2.VideoCapture that never opens
        return cap

    monkeypatch.setattr(cv2, "VideoCapture", factory)
    cam = HeadCamera()
    ok, frame = cam.read()
    assert ok is False
    assert frame is None


def test_already_small_frame_is_not_resized(fake_capture: _CapList) -> None:
    small = np.zeros((972, 1296, 3), dtype=np.uint8)
    assert fake_capture.set_frame is not None
    fake_capture.set_frame(lambda: small.copy())
    cam = HeadCamera(HeadCameraConfig(rotation=0))
    _, frame = cam.read()
    assert frame.shape == (972, 1296, 3)


def test_context_manager_opens_and_closes(fake_capture: _CapList) -> None:
    with HeadCamera() as cam:
        assert cam.is_open
    assert not cam.is_open


# ----------------------------------------------------------------------
# Background drain — fan-out of one camera to several readers.
# ----------------------------------------------------------------------


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_latest_starts_drain_and_returns_frame(fake_capture: _CapList) -> None:
    """latest() lazily starts the drain and returns what read() produced."""
    cam = HeadCamera(HeadCameraConfig(rotation=0))
    try:
        frame = cam.latest(timeout=2.0)
        assert frame is not None
        assert frame.shape == (972, 1296, 3)
    finally:
        cam.close()


def test_latest_returns_none_when_camera_cannot_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device that never opens: latest() times out to None instead of raising."""

    def factory(_device: Any) -> _FakeCap:
        cap = _FakeCap(_full_frame)
        cap._opened = False
        return cap

    monkeypatch.setattr(cv2, "VideoCapture", factory)
    cam = HeadCamera()
    try:
        assert cam.latest(timeout=0.3) is None
    finally:
        cam.close()


def test_add_consumer_receives_each_frame(fake_capture: _CapList) -> None:
    received: list[tuple[Any, float]] = []
    lock = threading.Lock()

    def consumer(frame: Any, ts: float) -> None:
        with lock:
            received.append((frame, ts))

    cam = HeadCamera(HeadCameraConfig(rotation=0))
    cam.add_consumer(consumer)
    try:
        cam.start_drain()
        assert _wait_until(lambda: len(received) >= 2)
        frame, ts = received[0]
        assert frame.shape == (972, 1296, 3)
        assert isinstance(ts, float)
    finally:
        cam.close()


def test_consumer_exception_does_not_stop_drain(fake_capture: _CapList) -> None:
    """One consumer raising doesn't stop the drain loop or later consumers."""
    good_calls: list[int] = []
    lock = threading.Lock()

    def boom(_frame: Any, _ts: float) -> None:
        raise RuntimeError("consumer broke")

    def good(_frame: Any, _ts: float) -> None:
        with lock:
            good_calls.append(1)

    cam = HeadCamera(HeadCameraConfig(rotation=0))
    cam.add_consumer(boom)
    cam.add_consumer(good)
    try:
        cam.start_drain()
        assert _wait_until(lambda: len(good_calls) >= 2)
    finally:
        cam.close()


def test_close_is_bounded_when_a_read_is_wedged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read wedged at the USB level does not block close() forever (the handle leaks instead).

    release must never race a read, and teardown must be bounded — otherwise
    Palmimo.disconnect() can never reach releasing the servo driver.
    """
    monkeypatch.setattr(camera_module, "_DRAIN_JOIN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(camera_module, "_CLOSE_LOCK_TIMEOUT_S", 0.1)
    wedge = threading.Event()
    events: list[str] = []

    class _WedgedCap:
        def isOpened(self) -> bool:  # noqa: N802 — mirrors cv2.VideoCapture
            return True

        def set(self, *_args: Any) -> bool:
            return True

        def read(self) -> tuple[bool, Any]:
            wedge.wait(timeout=5.0)  # simulates a USB-wedged read
            return False, None

        def release(self) -> None:
            events.append("release")

    monkeypatch.setattr(cv2, "VideoCapture", lambda _device: _WedgedCap())
    cam = HeadCamera(HeadCameraConfig(rotation=0))
    cam.start_drain()
    time.sleep(0.05)  # wait for the drain to enter the read
    t0 = time.monotonic()
    cam.close()
    assert time.monotonic() - t0 < 2.0  # join 0.05s + lock 0.1s + margin
    assert "release" not in events  # release must never race an in-flight read
    wedge.set()  # let the wedged read return -> exits quietly since its generation is already stopped


def test_restart_uses_a_fresh_stop_event_so_a_stopped_drain_cannot_revive(fake_capture: _CapList) -> None:
    """A stopped generation's stop event stays set; a fresh start uses a new event.

    If the shared event were reused via clear(), a thread abandoned by a join
    timeout could come back to life on a later start and double up the drain.
    A separate event per generation makes that impossible.
    """
    cam = HeadCamera(HeadCameraConfig(rotation=0))
    try:
        cam.start_drain()
        first_gen_stop = cam._drain_stop
        cam.stop_drain()
        assert first_gen_stop.is_set()  # old generation stays permanently signaled to stop
        cam.start_drain()
        assert cam._drain_stop is not first_gen_stop
        assert not cam._drain_stop.is_set()
        assert first_gen_stop.is_set()  # starting a new generation doesn't revive the old one
    finally:
        cam.close()


def test_latest_after_restart_does_not_return_a_pre_stop_frame(fake_capture: _CapList) -> None:
    """After stop -> restart, if no frame has arrived yet, latest() returns None.

    Returning a stale pre-stop frame as "latest" would make an observation right
    after recovery send a past scene to the VLM as if it were current. Freshness
    resets whenever the drain restarts.
    """
    cam = HeadCamera(HeadCameraConfig(rotation=0))
    try:
        assert cam.latest() is not None  # a frame flowed through and was cached at least once
        cam.stop_drain()
        fake_capture[0].fail = True  # camera can't be read after the restart
        cam.start_drain()
        assert cam.latest(timeout=0.1) is None
    finally:
        cam.close()


def test_close_releases_device_when_drain_thread_failed_to_start(
    fake_capture: _CapList, monkeypatch: pytest.MonkeyPatch
) -> None:
    """close() releases the device even when Thread.start() fails.

    Trying to join a Thread that failed to start raises RuntimeError, which
    would abort close() before release and leak the device — guard against
    that regression.
    """

    class _UnstartableThread(threading.Thread):
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(camera_module.threading, "Thread", _UnstartableThread)
    cam = HeadCamera(HeadCameraConfig(rotation=0))
    cam.open()
    with pytest.raises(RuntimeError, match="can't start new thread"):
        cam.start_drain()
    cam.close()  # must not raise
    assert cam.is_open is False


def test_latest_after_close_does_not_reopen_the_device(fake_capture: _CapList) -> None:
    """latest() after close() doesn't quietly come back to life — no device reopen, no drain restart, just None.

    Only an explicit open() re-arms the drain (Palmimo's reconnect cycle).
    """
    cam = HeadCamera(HeadCameraConfig(rotation=0))
    assert cam.latest() is not None
    cam.close()
    assert cam.latest(timeout=0.05) is None
    assert cam.is_open is False
    cam.open()
    try:
        assert cam.latest() is not None
    finally:
        cam.close()


def test_add_consumer_while_drain_is_running_receives_next_frame(fake_capture: _CapList) -> None:
    """A consumer registered while the drain is running still gets the next frame (registration and delivery run on separate threads)."""
    received: list[int] = []
    lock = threading.Lock()

    def late_consumer(_frame: Any, _ts: float) -> None:
        with lock:
            received.append(1)

    cam = HeadCamera(HeadCameraConfig(rotation=0))
    try:
        cam.start_drain()
        assert cam.latest() is not None  # confirm the drain is actually running
        cam.add_consumer(late_consumer)
        assert _wait_until(lambda: len(received) >= 1)
    finally:
        cam.close()


def test_start_drain_is_idempotent(fake_capture: _CapList) -> None:
    cam = HeadCamera(HeadCameraConfig(rotation=0))
    try:
        cam.start_drain()
        cam.start_drain()  # second call is a no-op, not a second thread
        assert _wait_until(lambda: cam.latest(timeout=0.1) is not None)
        assert len(fake_capture) == 1  # device opened once
    finally:
        cam.close()


def test_drain_sleeps_briefly_on_read_failure(fake_capture: _CapList, monkeypatch: pytest.MonkeyPatch) -> None:
    """A persistently failing grab backs off with a short sleep instead of
    busy-looping at 100% CPU."""
    import palmimo_sdk.io.camera as camera_module

    sleep_calls: list[float] = []
    real_time = camera_module.time

    class _FakeTime:
        @staticmethod
        def sleep(s: float) -> None:
            sleep_calls.append(s)

        @staticmethod
        def monotonic() -> float:
            return real_time.monotonic()

    cam = HeadCamera(HeadCameraConfig(rotation=0))
    cam.open()
    fake_capture[0].fail = True
    monkeypatch.setattr(camera_module, "time", _FakeTime())
    try:
        cam.start_drain()
        assert _wait_until(lambda: len(sleep_calls) >= 3)
        assert all(s > 0 for s in sleep_calls)
    finally:
        # restore the real time module before close()'s join(timeout=...) waits on it
        monkeypatch.setattr(camera_module, "time", real_time)
        cam.close()


def test_stop_drain_does_not_release_cv2(fake_capture: _CapList) -> None:
    """stop_drain() alone stops the thread but leaves the device open — only
    close() releases cv2 (mirrors close()'s stop-then-release contract)."""
    cam = HeadCamera(HeadCameraConfig(rotation=0))
    try:
        assert cam.latest(timeout=2.0) is not None
        cam.stop_drain()
        cam.stop_drain()  # idempotent
        assert cam.is_open
    finally:
        cam.close()


def test_close_waits_for_in_flight_read_before_releasing_cv2(monkeypatch: pytest.MonkeyPatch) -> None:
    """close() must not release cv2 while the drain thread is mid-grab.

    Simulates a ``cap.read()`` that blocks until told to return, starts the
    drain, waits until it is actually inside that blocking read, then calls
    close() from another thread. close() must not release cv2 until the
    in-flight read finishes — the shared lock has to serialize them even when
    ``stop_drain``'s join times out with the read still in flight.
    """
    in_read = threading.Event()
    allow_read_return = threading.Event()
    events: list[str] = []

    class _BlockingCap:
        def isOpened(self) -> bool:  # noqa: N802 — mirrors cv2.VideoCapture
            return True

        def set(self, *_args: Any) -> bool:
            return True

        def read(self) -> tuple[bool, Any]:
            events.append("read-start")
            in_read.set()
            allow_read_return.wait(timeout=2.0)
            events.append("read-end")
            return True, _full_frame()

        def release(self) -> None:
            events.append("release")

    monkeypatch.setattr(cv2, "VideoCapture", lambda _device: _BlockingCap())

    cam = HeadCamera()
    cam.start_drain()
    assert in_read.wait(timeout=2.0)  # drain thread is now inside cap.read()

    close_done = threading.Event()

    def _close() -> None:
        cam.close()
        close_done.set()

    closer = threading.Thread(target=_close)
    closer.start()
    time.sleep(0.1)  # give close() a chance to race — it must not have released yet
    assert "release" not in events
    assert close_done.is_set() is False

    allow_read_return.set()  # let the in-flight read complete
    closer.join(timeout=2.0)
    assert close_done.is_set()
    assert events == ["read-start", "read-end", "release"]
