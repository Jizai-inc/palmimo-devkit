"""Head camera capture: one background thread keeps the newest JPEG frame;
`mjpeg_generator` fans that single buffer out to any number of viewers.

Capturing once and letting every `/video.mjpeg` connection read the same
buffer (rather than each pulling its own frame from `HeadCamera`) is what
lets N browser tabs share one camera without N times the capture/encode
cost -- `HeadCamera` itself only supports one open device per process anyway
(see `palmimo_sdk.io.camera`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from palmimo_sdk import HeadCamera


_LOG = logging.getLogger(__name__)

#: Target capture rate. The head camera's own README/spec figure ("~15fps"
#: over UVC at this resolution) is a ceiling this only aims for, not a
#: guarantee -- `VideoStream.camera_ok` is the truth signal, not this constant.
CAPTURE_INTERVAL_S: float = 1.0 / 15
JPEG_QUALITY: int = 80
FRAME_WIDTH: int = 640
FRAME_HEIGHT: int = 480
MJPEG_BOUNDARY: str = "palmimoframe"

#: Minimum gap between consecutive "capture failed" log lines once a failure
#: is already being logged, so a persistently failing camera logs at roughly
#: this rate instead of once per capture attempt (up to 15/s).
FAILURE_LOG_INTERVAL_S: float = 5.0

#: Default `stop()` join timeout before skipping `camera.close()`.
STOP_TIMEOUT_S: float = 2.0


def encode_jpeg(frame: Any) -> bytes:
    """Downscale *frame* to `FRAME_WIDTH`x`FRAME_HEIGHT` and JPEG-encode it.

    `cv2` is imported here rather than at module level so importing
    `video.py` stays possible without the `vision` extra installed (matching
    `palmimo_sdk.io.camera`'s own lazy-import discipline) -- only actually
    calling this (from the background capture thread, once a camera exists)
    requires it.
    """
    import cv2

    resized = cv2.resize(frame, (FRAME_WIDTH, FRAME_HEIGHT))
    ok, buf = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise RuntimeError("cv2.imencode failed to produce a JPEG")
    return buf.tobytes()


class VideoStream:
    """Owns a `HeadCamera`'s open/close lifecycle, its background capture thread, and the
    latest-JPEG buffer that thread fills.

    Owning `camera.open()`/`camera.close()` itself, independent of `Palmimo`,
    is what lets a camera that fails to open leave the rest of the robot
    (servo bus, WS control) unaffected: `Palmimo.connect()` rolls back every
    resource it opened if any one of them raises (see
    `palmimo_sdk.robot.Palmimo.connect`), which a camera wired in as
    `Palmimo(camera=...)` would trigger on exactly the failure this class is
    meant to degrade gracefully from.

    *camera* is `None` under `--no-camera` -- `start()` is then a no-op,
    `has_camera`/`camera_ok` stay `False` forever, and `latest_jpeg()` always
    returns `None`.
    """

    def __init__(
        self,
        camera: HeadCamera | None,
        *,
        capture_interval: float = CAPTURE_INTERVAL_S,
        encode: Callable[[Any], bytes] = encode_jpeg,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
        stop_timeout: float = STOP_TIMEOUT_S,
    ) -> None:
        self._camera = camera
        self._capture_interval = capture_interval
        self._encode = encode
        self._sleep = sleep
        self._now = now
        self._stop_timeout = stop_timeout
        self._lock = threading.Lock()
        self._latest_jpeg: bytes | None = None
        self._camera_ok = False
        self._available = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_failure_log_at: float | None = None

    @property
    def has_camera(self) -> bool:
        """Whether the camera opened successfully -- `False` under `--no-camera` and also
        after an open failure, so `/video.mjpeg` 503s in both cases rather than only the former."""
        return self._available

    @property
    def camera_ok(self) -> bool:
        """Whether the most recent capture attempt produced a frame."""
        with self._lock:
            return self._camera_ok

    def latest_jpeg(self) -> bytes | None:
        """The newest encoded frame, or `None` before the first successful capture (or with no camera)."""
        with self._lock:
            return self._latest_jpeg

    def start(self) -> None:
        """Open the camera and start the background capture thread.

        A no-op if already running -- including a thread `stop()` failed to
        join within `stop_timeout`: that thread's handle is kept (not
        cleared) precisely so this check still sees it and refuses to start
        a second capture thread alongside it. With no camera attached, or if
        `open()` raises (device busy, not present, `vision` extra missing),
        logs a warning and returns without starting a thread -- `has_camera`
        stays `False` rather than the exception propagating out of the
        server lifespan.
        """
        if self._camera is None or (self._thread is not None and self._thread.is_alive()):
            return
        try:
            self._camera.open()
        except Exception:
            _LOG.exception("failed to open head camera -- video disabled")
            return
        self._available = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="palmimo-teleop-video", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background capture thread and close the camera. Safe to call whether or not
        `start()` succeeded.

        If the thread does not stop within `stop_timeout`, `camera.close()`
        is skipped and an error is logged instead of closing it anyway --
        closing the device out from under a still-running capture thread
        would race the close against whatever read is in flight. The thread
        handle is kept (not cleared) in this case, both so `start()` can
        refuse to start a second capture thread and so a still-alive thread
        never leaves the camera looking usable: `has_camera` still flips to
        `False`, since nothing is refreshing `latest_jpeg()`'s buffer any
        more even though the device itself stays open.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._stop_timeout)
            if self._thread.is_alive():
                _LOG.error(
                    "video capture thread did not stop within %.1fs -- leaving the camera "
                    "open rather than closing it while capture may still be running",
                    self._stop_timeout,
                )
                self._available = False
                return
            self._thread = None
        if self._camera is not None and self._available:
            with contextlib.suppress(Exception):
                self._camera.close()
        self._available = False

    def _log_capture_failure(self) -> None:
        now = self._now()
        if self._last_failure_log_at is None or now - self._last_failure_log_at >= FAILURE_LOG_INTERVAL_S:
            _LOG.exception("head camera capture failed")
            self._last_failure_log_at = now

    def _run(self) -> None:
        camera = self._camera
        assert camera is not None
        while not self._stop_event.is_set():
            try:
                frame = camera.latest(timeout=self._capture_interval)
                if frame is None:
                    raise RuntimeError("head camera produced no frame")
                encoded = self._encode(frame)
            except Exception:
                with self._lock:
                    self._camera_ok = False
                self._log_capture_failure()
                self._sleep(self._capture_interval)
                continue
            with self._lock:
                self._latest_jpeg = encoded
                self._camera_ok = True
            self._sleep(self._capture_interval)


async def mjpeg_generator(video: VideoStream, *, interval: float = CAPTURE_INTERVAL_S) -> AsyncIterator[bytes]:
    """Yield one `multipart/x-mixed-replace` chunk per *interval*, reading `video`'s current buffer.

    Runs once per `/video.mjpeg` connection; every instance reads the same
    `VideoStream`, so viewer count never multiplies capture/encode work.
    Frames already at `video.latest_jpeg()` at the time this is first called
    are served immediately -- it never waits for a fresh capture.
    """
    while True:
        frame = video.latest_jpeg()
        if frame is not None:
            yield (
                b"--" + MJPEG_BOUNDARY.encode() + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
            )
        await asyncio.sleep(interval)
