# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Head camera I/O resource — the single owner of the head camera's UVC capture.

Mirrors the servo I/O layer (:mod:`palmimo_sdk.io.dynamixel`): the SDK owns the
hardware resource, and higher layers consume it instead of opening OpenCV
themselves.

The head camera only delivers the full field of view at its native 4:3
mode (``capture_width`` x ``capture_height``, MJPG). Any smaller / 16:9 request
is served as a top-left corner crop — a narrow, off-center "looking down" view,
not a scaled-down full frame. So :meth:`HeadCamera.read` always grabs the full
sensor and downscales every frame to ``proc_width`` x ``proc_height`` in
software. The head camera is mounted upside down, so frames are rotated by
``rotation`` degrees (180 by default) to come out upright.

Two independent lifecycles:

  - **cv2 device** — :meth:`open` (idempotent) / :meth:`read` (auto-opens) /
    :meth:`close`. A single consumer running its own tracking loop drives
    this directly and nothing else runs in the background.
  - **Background drain** — :meth:`start_drain` / :meth:`stop_drain` (both
    idempotent), opt-in for fanning one physical camera out to several
    readers (e.g. an on-demand still capture and a continuous frame watcher
    sharing one :class:`HeadCamera` instead of opening ``/dev/video`` twice).
    A daemon thread calls :meth:`read` in a loop, keeps the newest frame for
    :meth:`latest`, and dispatches every frame to callbacks registered via
    :meth:`add_consumer` (consumer exceptions are logged and swallowed so one
    bad detector never stops the read loop). :meth:`latest` lazily starts the
    drain if it isn't running yet, so a standalone caller (no owner calling
    :meth:`start_drain` up front) still works — until :meth:`close`, after
    which only an explicit :meth:`open` re-arms the drain (a late
    :meth:`latest` returns ``None`` instead of silently reopening the device).

Thread safety: a single ``threading.Lock`` (``_cap_lock``) serializes every
call into the underlying ``cv2.VideoCapture`` — :meth:`read`'s grab and
:meth:`close`'s release can never run concurrently. :meth:`close` always calls
:meth:`stop_drain` first; if the drain thread is mid-grab when
:meth:`stop_drain`'s ``join`` times out, :meth:`close` still cannot race it —
it waits on ``_cap_lock`` until that in-flight ``read()`` returns, and the
drain loop checks its stop flag (set before the join, so before any lock
contention) before ever grabbing again. The wait is bounded
(``_CLOSE_LOCK_TIMEOUT_S``): a read wedged at the USB level past the budget
makes :meth:`close` leak the handle (logged) rather than block forever — the
release is either serialized after the read or skipped entirely, never
concurrent with it, and teardown stays bounded either way.

``cv2`` (OpenCV) is an optional dependency (``palmimo-sdk[vision]``), imported
lazily so importing this module stays hardware-free for compute-only use,
matching :class:`~palmimo_sdk.io.dynamixel.DynamixelDriver`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    import cv2


_ROTATIONS = (0, 90, 180, 270)

# Bounded-teardown budgets. stop_drain waits this long for the drain thread to
# exit; close() then waits this long for the device lock. Both bounded so a
# camera wedged in a USB-level read can never stall Palmimo.disconnect() (which
# must go on to release the servo driver).
_DRAIN_JOIN_TIMEOUT_S = 2.0
_CLOSE_LOCK_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class HeadCameraConfig:
    """Capture settings for the head camera.

    ``rotation`` is degrees clockwise applied after downscaling; it generalizes
    the upside-down head mount (180). It is mount-dependent — verify the value
    against the camera's final mounted orientation, it is not a fixed property
    of the sensor.
    """

    device: int | str = 0
    capture_width: int = 2592
    capture_height: int = 1944
    proc_width: int = 1296
    proc_height: int = 972
    rotation: int = 180

    def __post_init__(self) -> None:
        if self.rotation not in _ROTATIONS:
            raise ValueError(f"rotation must be one of {_ROTATIONS}, got {self.rotation}")


class HeadCamera:
    """OpenCV-backed head camera that owns one ``cv2.VideoCapture``.

    Lifecycle: :meth:`open` (idempotent) / :meth:`read` (auto-opens) /
    :meth:`close`. Usable as a context manager. Only one process may hold the
    device at a time, so a single :class:`HeadCamera` instance is the intended
    owner per session.

    An optional background drain (:meth:`start_drain` / :meth:`stop_drain` /
    :meth:`add_consumer` / :meth:`latest`) lets several readers share that one
    instance instead of each opening their own device — see the module
    docstring for the concurrency contract.
    """

    def __init__(self, config: HeadCameraConfig | None = None) -> None:
        self.config = config or HeadCameraConfig()
        self._cap: cv2.VideoCapture | None = None
        # Serializes every cv2.VideoCapture call (open/read/release) so the
        # drain thread's read() and a caller's close() can never touch the
        # capture object at the same time.
        self._cap_lock = threading.Lock()

        # Background drain state (opt-in — see start_drain/stop_drain).
        self._latest_lock = threading.Lock()
        self._latest: Any | None = None
        # Guards _consumers: registration and the drain loop's fan-out run on
        # different threads. CPython's GIL happens to make the bare list safe
        # today, but the discipline should not depend on it (free-threaded
        # builds won't have it).
        self._consumers_lock = threading.Lock()
        self._consumers: list[Callable[[Any, float], None]] = []
        # Guards the drain lifecycle state (_drain_thread / _drain_stop) so two
        # start_drain() calls cannot both pass the idempotency check. The stop
        # event is per-generation: each drain thread closes over its own, so a
        # thread whose join timed out can never be revived by a later start
        # clearing a shared flag.
        self._drain_lock = threading.Lock()
        self._drain_thread: threading.Thread | None = None
        self._drain_stop = threading.Event()
        self._first_frame = threading.Event()
        self._closed = False  # close() is terminal for the drain until open()
        self._log = logging.getLogger(__name__)

    @property
    def is_open(self) -> bool:
        cap = self._cap  # snapshot: close() may null the attribute between the two uses
        return cap is not None and cap.isOpened()

    def open(self) -> None:
        self._closed = False  # an explicit open re-arms the drain after close()
        with self._cap_lock:
            self._open_locked()

    def _open_locked(self) -> None:
        """Actual cv2 open. Caller must hold ``_cap_lock``."""
        if self.is_open:
            return
        import cv2

        cap = cv2.VideoCapture(self.config.device)
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open camera {self.config.device}")
        # Full sensor in MJPG; a smaller request would be a corner crop instead
        # of a scaled full frame (see module docstring).
        # VideoWriter.fourcc, unlike the VideoWriter_fourcc alias, is present in
        # the cv2 stubs, so this type-checks whether or not opencv is installed.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.capture_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.capture_height)
        self._cap = cap

    def read(self) -> tuple[bool, Any]:
        """Grab one frame, downscaled to the processing size and rotated upright.

        Returns ``(ok, frame)`` so callers can skip a failed grab without
        raising; ``frame`` is ``None`` when ``ok`` is False. A failure to open
        the device is reported the same way rather than propagating.

        The open + grab happen under ``_cap_lock``; the downscale/rotate run
        afterwards so the lock is only held for the actual device I/O (see
        module docstring for why this matters for :meth:`close`).
        """
        import cv2

        with self._cap_lock:
            try:
                self._open_locked()
            except RuntimeError:
                return False, None
            assert self._cap is not None  # _open_locked sets it (or raised, handled above)
            ok, frame = self._cap.read()
        if not ok or frame is None:
            return False, None
        # Skip resize if the backend ignored our request and already returned small.
        if frame.shape[1] > self.config.proc_width:
            frame = cv2.resize(
                frame,
                (self.config.proc_width, self.config.proc_height),
                interpolation=cv2.INTER_AREA,
            )
        if self.config.rotation:
            frame = cv2.rotate(frame, _rotate_code(cv2, self.config.rotation))
        return True, frame

    def close(self) -> None:
        """Stop the drain (if running), then release the cv2 device — bounded.

        Stopping first — and the shared ``_cap_lock`` — is what makes this
        safe against an in-flight background read; see the module docstring.
        The lock wait is bounded (``_CLOSE_LOCK_TIMEOUT_S``): if a read is
        wedged at the USB level and never returns, the handle is deliberately
        LEAKED (with an error log) instead of blocking forever — releasing
        under an in-flight read is undefined behaviour in OpenCV, and an
        unbounded wait here would stall ``Palmimo.disconnect()`` before it ever
        reaches the servo driver.

        Terminal for the drain: a later :meth:`latest` / :meth:`start_drain`
        will NOT silently restart it (and :meth:`latest` returns ``None``
        instead of a stale pre-close frame) — only an explicit :meth:`open`
        re-arms. :meth:`read`'s one-shot auto-open is unchanged.
        """
        self._closed = True  # set first so a racing latest() cannot restart the drain
        self.stop_drain()
        if self._cap_lock.acquire(timeout=_CLOSE_LOCK_TIMEOUT_S):
            try:
                if self._cap is not None:
                    self._cap.release()
                    self._cap = None
            finally:
                self._cap_lock.release()
        else:
            self._log.error(
                "HeadCamera.close: a device read is wedged past %.1fs; leaking the cv2 "
                "handle so teardown stays bounded",
                _CLOSE_LOCK_TIMEOUT_S,
            )
        self._first_frame.clear()
        with self._latest_lock:
            self._latest = None

    def __enter__(self) -> HeadCamera:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Background drain — opt-in fan-out for multiple readers of one camera.
    # ------------------------------------------------------------------
    def add_consumer(self, fn: Callable[[Any, float], None]) -> None:
        """Register a callback invoked with every frame drained (background thread).

        ``fn(frame, timestamp)`` where ``timestamp`` is ``time.monotonic()``. An
        exception from ``fn`` is logged and swallowed; the drain loop keeps
        running. Safe to call while the drain is running — the new consumer
        starts receiving from the next frame.
        """
        with self._consumers_lock:
            self._consumers.append(fn)

    def start_drain(self) -> None:
        """Start the background drain thread (idempotent; no-op if already running).

        Refused after :meth:`close` — see there.
        """
        with self._drain_lock:
            if self._closed or self._drain_thread is not None:
                return
            stop = threading.Event()
            self._drain_stop = stop
            self._first_frame.clear()
            # A frame captured before a stop/restart is not "the latest" anymore:
            # returning it would present an old scene as current when the camera
            # fails to deliver after the restart. Freshness restarts with the drain.
            with self._latest_lock:
                self._latest = None
            # Assign only after a successful start: if Thread.start() raises (e.g.
            # resource exhaustion), a later close() must not try to join a
            # never-started thread — that join raises and would abort close()
            # before it releases the device.
            thread = threading.Thread(
                target=self._drain_loop, args=(stop,), name="palmimo-headcamera-drain", daemon=True
            )
            thread.start()
            self._drain_thread = thread

    def stop_drain(self) -> None:
        """Stop the background drain thread (idempotent). Does not touch cv2.

        The join is bounded; a thread wedged in a device read past the timeout
        is abandoned, but its own (already set) stop event guarantees it exits
        without another grab once the read returns — a later restart creates a
        fresh event, so the abandoned thread can never be revived.
        """
        with self._drain_lock:
            self._drain_stop.set()
            thread = self._drain_thread
            self._drain_thread = None
        if thread is not None:
            thread.join(timeout=_DRAIN_JOIN_TIMEOUT_S)

    def latest(self, *, timeout: float = 2.0) -> Any | None:
        """Return the newest drained frame, starting the drain if needed.

        Waits up to *timeout* seconds for a first frame if the drain just
        started; returns ``None`` if none arrives in time (e.g. the device
        can't open).
        """
        if self._drain_thread is None:
            self.start_drain()
        self._first_frame.wait(timeout)
        with self._latest_lock:
            return self._latest

    def _drain_loop(self, stop: threading.Event) -> None:
        """Background thread body: read() continuously, update latest, fan out.

        Checks *stop* — this generation's own event, closed over at start — so
        once :meth:`stop_drain` sets it there is no further ``read()`` call
        (the invariant :meth:`close` relies on to release cv2 safely), and a
        later restart cannot revive this thread.
        """
        while not stop.is_set():
            ok, frame = self.read()
            if not ok or frame is None:
                # Without a wait here, a persistent read failure spins at
                # 100% CPU. A short sleep gives the camera time to recover.
                time.sleep(0.1)
                continue
            with self._latest_lock:
                self._latest = frame
            self._first_frame.set()
            ts = time.monotonic()
            # Snapshot under the lock, dispatch outside it — a callback that
            # registers another consumer must not deadlock.
            with self._consumers_lock:
                consumers = list(self._consumers)
            for fn in consumers:
                try:
                    fn(frame, ts)
                except Exception:
                    self._log.exception("frame consumer failed")


def _rotate_code(cv2: Any, rotation: int) -> int:
    return {
        90: cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }[rotation]
