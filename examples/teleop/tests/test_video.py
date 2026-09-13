"""Behavior of `palmimo_teleop.video.VideoStream`, against a fake camera and a fake
encoder -- no `cv2`, no real device, no real time."""

import logging
import threading
import time
from typing import Any

import pytest

from palmimo_teleop.video import FAILURE_LOG_INTERVAL_S, VideoStream


class FakeCamera:
    def __init__(self, frames: list[Any] | None = None, *, fail_open: bool = False) -> None:
        self._frames = frames if frames is not None else ["frame"]
        self._fail_open = fail_open
        self.opened = False
        self.closed = False

    def open(self) -> None:
        if self._fail_open:
            raise RuntimeError("camera not present")
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def latest(self, *, timeout: float) -> Any | None:
        return self._frames[0] if self._frames else None


def _fake_encode(frame: Any) -> bytes:
    return f"jpeg:{frame}".encode()


def _no_sleep(_: float) -> None:
    return None


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_start_with_no_camera_never_marks_available() -> None:
    # Without this, `--no-camera` would have no defined behavior for callers
    # checking whether video is available.
    video = VideoStream(None)
    video.start()
    assert video.has_camera is False
    assert video.latest_jpeg() is None


def test_start_with_failing_camera_open_degrades_without_raising() -> None:
    # Without this, a camera that fails to open (missing device, missing
    # `vision` extra) would crash the whole server instead of starting
    # compute-only with video disabled.
    video = VideoStream(FakeCamera(fail_open=True))
    video.start()
    assert video.has_camera is False


def test_start_with_working_camera_populates_the_jpeg_buffer() -> None:
    # Without this, `/video.mjpeg` would have nothing to serve even with a
    # working camera attached.
    camera = FakeCamera(frames=["a"])
    video = VideoStream(camera, encode=_fake_encode, sleep=_no_sleep)
    video.start()
    try:
        deadline = time.monotonic() + 2.0
        while video.latest_jpeg() is None and time.monotonic() < deadline:
            pass
        assert video.latest_jpeg() == b"jpeg:a"
        assert video.has_camera is True
        assert video.camera_ok is True
    finally:
        video.stop()


def test_capture_failure_marks_camera_not_ok_without_stopping_the_thread() -> None:
    # Without this, one bad frame (or a transient read error) would
    # permanently wedge `camera_ok` instead of recovering on the next capture.
    calls = {"count": 0}

    def flaky_encode(frame: Any) -> bytes:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("encode failed")
        return _fake_encode(frame)

    camera = FakeCamera(frames=["a"])
    video = VideoStream(camera, encode=flaky_encode, sleep=_no_sleep)
    video.start()
    try:
        deadline = time.monotonic() + 2.0
        while video.latest_jpeg() is None and time.monotonic() < deadline:
            pass
        assert video.latest_jpeg() == b"jpeg:a"
        assert video.camera_ok is True
    finally:
        video.stop()


def test_stop_closes_the_camera() -> None:
    # Without this, the camera device would stay open (and its background
    # drain thread running) after the server shuts down.
    camera = FakeCamera()
    video = VideoStream(camera, encode=_fake_encode, sleep=_no_sleep)
    video.start()
    video.stop()
    assert camera.closed is True


def test_stop_with_no_camera_is_a_no_op() -> None:
    # Without this, shutting down a `--no-camera` server could raise instead
    # of cleanly no-op'ing.
    video = VideoStream(None)
    video.start()
    video.stop()  # must not raise


def test_stop_skips_closing_the_camera_when_the_thread_does_not_stop_in_time() -> None:
    # Without this, `stop()` could close the camera device out from under a
    # still-running capture thread, racing the close against a read in flight.
    camera = FakeCamera()
    video = VideoStream(camera, stop_timeout=0.05)
    video._thread = threading.Thread(target=lambda: time.sleep(1.0), daemon=True)
    video._thread.start()
    video.stop()
    assert camera.closed is False


def test_stop_marks_the_camera_unavailable_when_the_thread_does_not_stop_in_time() -> None:
    # Without this, `/video.mjpeg` would keep 200ing off the last-captured
    # buffer (via `has_camera`) after a stuck stop(), even though nothing is
    # refreshing that buffer any more and the camera was never actually closed.
    camera = FakeCamera()
    video = VideoStream(camera, encode=_fake_encode, sleep=_no_sleep, stop_timeout=0.05)
    video.start()
    assert video.has_camera is True
    video._thread = threading.Thread(target=lambda: time.sleep(1.0), daemon=True)
    video._thread.start()
    video.stop()
    assert video.has_camera is False


def test_start_refuses_a_second_thread_after_a_timed_out_stop() -> None:
    # Without this, a capture thread wedged past stop_timeout would let the
    # next start() spin up a second capture thread on the same camera --
    # the stop-timeout branch used to clear the thread handle, leaving
    # start() no way to tell the old thread was still alive.
    camera = FakeCamera()
    video = VideoStream(camera, stop_timeout=0.05)
    stuck_thread = threading.Thread(target=lambda: time.sleep(1.0), name="palmimo-teleop-video", daemon=True)
    video._thread = stuck_thread
    video._available = True
    stuck_thread.start()
    video.stop()

    video.start()

    running = [t for t in threading.enumerate() if t.name == "palmimo-teleop-video"]
    assert running == [stuck_thread]
    stuck_thread.join(timeout=2.0)


def test_capture_failure_logging_is_throttled(caplog: pytest.LogCaptureFixture) -> None:
    # Without this, a persistently failing camera would log one line per
    # capture attempt (up to 15/s) instead of roughly once per FAILURE_LOG_INTERVAL_S.
    clock = FakeClock()
    video = VideoStream(FakeCamera(), now=clock)
    with caplog.at_level(logging.ERROR, logger="palmimo_teleop.video"):
        video._log_capture_failure()  # logged immediately (first failure)
        clock.now += FAILURE_LOG_INTERVAL_S / 2
        video._log_capture_failure()  # inside the throttle window -- not logged
        clock.now += FAILURE_LOG_INTERVAL_S
        video._log_capture_failure()  # past the window -- logged again
    assert len(caplog.records) == 2


def test_start_is_idempotent_while_running() -> None:
    # Without this, a duplicate `start()` call could spawn a second capture
    # thread racing the first over the same camera device.
    camera = FakeCamera(frames=["a"])
    video = VideoStream(camera, encode=_fake_encode, sleep=_no_sleep)
    video.start()
    threads_before = threading.active_count()
    video.start()
    try:
        assert threading.active_count() == threads_before
    finally:
        video.stop()
