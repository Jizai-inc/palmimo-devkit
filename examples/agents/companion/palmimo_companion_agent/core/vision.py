"""Camera-consumer assembly: wave / face detectors plus :class:`VisionWatch`.

MediaPipe HandLandmarker for "someone is waving"
(:class:`WaveDetector`) and MediaPipe FaceDetector for "where is the largest
face" (:class:`FaceLocator`, consumed by
:class:`~palmimo_companion_agent.core.tools.LookAtFaceTool`).
:class:`VisionWatch` registers itself as
a consumer on the SDK's own :class:`~palmimo_sdk.io.camera.HeadCamera` (via
``add_consumer``, which the facade's ``connect()`` already starts draining) and
yields debounced :class:`Detection` objects for
:class:`~palmimo_companion_agent.core.reflexes.ReflexEngine` to react to.

``mediapipe`` / ``cv2`` (the ``vision`` extra) are imported lazily inside
:class:`WaveDetector.__init__` / :class:`FaceLocator.__init__`, so importing
this module (and therefore ``palmimo_companion_agent``) never requires them --
only constructing a real detector does.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

from .perception import Detection, DetectionKind


_log = logging.getLogger(__name__)


# Detection / DetectionKind live in perception.py, shared by both senses;
# re-exported here so `from .vision import Detection` still reads naturally
# at the sight-flavoured call sites.


class Detector(Protocol):
    """A stateful per-frame detector :class:`VisionWatch` drives.

    Implementations are stateful (background tracking, judge history) and are
    only ever called from :class:`VisionWatch`'s single frame-consumer
    callback, so no internal locking is needed.
    """

    kind: ClassVar[DetectionKind]

    def process(self, frame: Any, timestamp: float) -> Detection | None:
        """Feed one frame; return a :class:`Detection` if it just fired, else ``None``."""
        ...


# ----------------------------------------------------------------------
# WaveDetector -- MediaPipe HandLandmarker + reversal-count wave judge
# ----------------------------------------------------------------------

_HAND_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/latest/hand_landmarker.task"
)
_DEFAULT_HAND_MODEL = str(Path.home() / "hand_landmarker.task")

# Palm-center landmarks (wrist + the four finger MCPs).
_PALM_LANDMARKS = (0, 5, 9, 13, 17)

# Tuned on real hardware: balances quick detection against false positives
# at a modest camera frame rate.
_MOVE_THRESHOLD = 0.025  # frame-to-frame x diff (fraction of frame width) counted as a "swing"
_WAVE_COUNT = 2  # direction reversals needed to call it a wave
_CLEAR_COUNT = 5  # consecutive missed frames before a track's history resets / the track is dropped

_PROC_WIDTH = 320  # width frames are downscaled to before MediaPipe inference

# Spatial-match distance (normalized) used to associate this frame's detected
# palms with last frame's tracks -- MediaPipe HandLandmarker hands back no
# persistent track id, and its Left/Right handedness label can repeat or
# swap between frames, so proximity is the next-best correlation signal.
_MATCH_MAX_DIST = 0.35


def _ensure_hand_model(path: str) -> None:
    """Download the HandLandmarker model to *path* if it isn't there already."""
    if Path(path).exists():
        return
    from palmimo_sdk.download import download_atomic

    download_atomic(_HAND_MODEL_URL, path, timeout=30.0)


class _ReversalWaveJudge:
    """Per-track wave judge: counts palm-x direction reversals across frames.

    Pure Python (no MediaPipe/cv2 dependency) so it is directly unit-testable.
    """

    def __init__(
        self,
        move_threshold: float = _MOVE_THRESHOLD,
        wave_count_threshold: int = _WAVE_COUNT,
        clear_count_threshold: int = _CLEAR_COUNT,
        max_history: int = 20,
    ) -> None:
        self.move_threshold = move_threshold
        self.wave_count_threshold = wave_count_threshold
        self.clear_count_threshold = clear_count_threshold
        self.x_history: deque[float] = deque(maxlen=max_history)
        self.prev_direction: int | None = None
        self.wave_count = 0
        self.clear_count = 0

    def update(self, x: float | None) -> bool:
        """Feed one frame's palm-center x (``None`` = no hand this frame); True on a wave."""
        if x is None:
            self.clear_count += 1
            if self.clear_count >= self.clear_count_threshold:
                self.x_history.clear()
                self.prev_direction = None
                self.wave_count = 0
            return False
        self.clear_count = 0
        self.x_history.append(x)
        if len(self.x_history) >= 2:
            diff = self.x_history[-1] - self.x_history[-2]
            if abs(diff) > self.move_threshold:
                current = 1 if diff > 0 else -1
                if self.prev_direction is not None and current != self.prev_direction:
                    self.wave_count += 1
                self.prev_direction = current
        if self.wave_count >= self.wave_count_threshold:
            self.wave_count = 0
            self.x_history.clear()
            self.prev_direction = None
            return True
        return False


@dataclass
class _HandTrack:
    """One physical hand: its judge plus the last palm position (for matching)."""

    judge: _ReversalWaveJudge
    x: float
    y: float
    lost_frames: int = 0


class WaveDetector:
    """Detects a waving hand via MediaPipe HandLandmarker + :class:`_ReversalWaveJudge`.

    :meth:`process` is split into :meth:`_locate_palms` (the MediaPipe/cv2
    call) and :meth:`_update` (pure-Python track matching + judging) so tests
    can exercise the tracking/judging logic without a real model -- construct
    with ``object.__new__(WaveDetector)`` and set ``_hands = []`` directly,
    then call :meth:`_update` with hand-picked palm positions.
    """

    kind: ClassVar[DetectionKind] = DetectionKind.WAVE

    def __init__(self, model_path: str = _DEFAULT_HAND_MODEL) -> None:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python.vision import HandLandmarker, HandLandmarkerOptions, RunningMode

        _ensure_hand_model(model_path)
        self._cv2 = cv2
        self._mp = mp
        options = HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=model_path),
            running_mode=RunningMode.VIDEO,
            num_hands=2,
            min_hand_detection_confidence=0.7,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self._landmarker = HandLandmarker.create_from_options(options)
        self._ts_ms = 0
        self._proc_h: int | None = None
        self._hands: list[_HandTrack] = []

    def process(self, frame: Any, timestamp: float) -> Detection | None:
        palms = self._locate_palms(frame, timestamp)
        return self._update(palms)

    def _locate_palms(self, frame: Any, timestamp: float) -> list[tuple[float, float]]:
        """Run MediaPipe HandLandmarker on *frame*; return each hand's palm-center (x, y)."""
        cv2 = self._cv2
        if self._proc_h is None:
            self._proc_h = max(1, round(frame.shape[0] * _PROC_WIDTH / frame.shape[1]))
        small = cv2.resize(frame, (_PROC_WIDTH, self._proc_h))
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        mp_image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        # VIDEO mode requires a strictly increasing millisecond timestamp.
        ts = max(self._ts_ms + 1, int(timestamp * 1000))
        self._ts_ms = ts
        result = self._landmarker.detect_for_video(mp_image, ts)
        palms = []
        for lms in result.hand_landmarks or []:
            px = sum(lms[k].x for k in _PALM_LANDMARKS) / len(_PALM_LANDMARKS)
            py = sum(lms[k].y for k in _PALM_LANDMARKS) / len(_PALM_LANDMARKS)
            palms.append((px, py))
        return palms

    def _match_track(self, px: float, py: float, matched: set[int]) -> int | None:
        """Return the closest not-yet-matched track within :data:`_MATCH_MAX_DIST`, if any."""
        best_idx: int | None = None
        best_dist = _MATCH_MAX_DIST
        for idx, track in enumerate(self._hands):
            if idx in matched:
                continue
            dist = math.hypot(px - track.x, py - track.y)
            if dist < best_dist:
                best_dist = dist
                best_idx = idx
        return best_idx

    def _update(self, palms: Iterable[tuple[float, float]]) -> Detection | None:
        """Match this frame's palms to tracks, judge each, and report the first wave found."""
        detected_x: float | None = None
        matched: set[int] = set()
        for px, py in palms:
            idx = self._match_track(px, py, matched)
            if idx is None:
                self._hands.append(_HandTrack(_ReversalWaveJudge(), px, py))
                idx = len(self._hands) - 1
            track = self._hands[idx]
            track.x, track.y = px, py
            track.lost_frames = 0
            matched.add(idx)
            if track.judge.update(px) and detected_x is None:
                detected_x = px

        for idx, track in enumerate(self._hands):
            if idx not in matched:
                track.judge.update(None)
                track.lost_frames += 1
        self._hands = [t for t in self._hands if t.lost_frames < _CLEAR_COUNT]

        if detected_x is None:
            return None
        side = "the right" if detected_x >= 0.5 else "the left"
        return Detection(summary=f"someone is waving on {side} side", kind=self.kind)


# ----------------------------------------------------------------------
# FaceLocator -- MediaPipe FaceDetector, largest-face normalized center
# ----------------------------------------------------------------------

_FACE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_detector/"
    "blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
)
_DEFAULT_FACE_MODEL = str(Path.home() / "blaze_face_short_range.tflite")


def _ensure_face_model(path: str) -> None:
    if Path(path).exists():
        return
    from palmimo_sdk.download import download_atomic

    download_atomic(_FACE_MODEL_URL, path, timeout=15.0)


class FaceLocator:
    """Locates the largest face's normalized ``(cx, cy)`` per frame (no debouncing).

    Not a :class:`Detector`: this is a plain per-frame locator
    :class:`~palmimo_companion_agent.core.tools.LookAtFaceTool`'s tracking
    loop polls directly for its error signal, so it does not go through
    :class:`VisionWatch`.

    :meth:`locate` is split into :meth:`_detect` (MediaPipe/cv2) and
    :meth:`_largest_center` (pure geometry) for the same testability reason as
    :class:`WaveDetector`.
    """

    def __init__(self, model_path: str | None = None, *, min_confidence: float = 0.4, proc_width: int = 640) -> None:
        import cv2
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision

        self._cv2 = cv2
        self._mp_vision = vision
        self._proc_width = proc_width
        # One locator instance is shared between look_at_face's tracking loop
        # and FacePresenceDetector on the camera drain thread; MediaPipe
        # detectors are not safe for concurrent detect calls, so serialize.
        self._lock = threading.Lock()
        path = model_path or _DEFAULT_FACE_MODEL
        _ensure_face_model(path)
        self._detector = vision.FaceDetector.create_from_options(
            vision.FaceDetectorOptions(
                base_options=python.BaseOptions(model_asset_path=path),
                running_mode=vision.RunningMode.IMAGE,
                min_detection_confidence=min_confidence,
            )
        )

    def locate(self, frame: Any) -> tuple[float, float] | None:
        """Return the largest detected face's normalized ``(cx, cy)``, or ``None``."""
        with self._lock:
            detections, w, h = self._detect(frame)
        return self._largest_center(detections, w, h)

    def _detect(self, frame: Any) -> tuple[list[Any], int, int]:
        import mediapipe as mp

        cv2 = self._cv2
        h, w = frame.shape[:2]
        sw = self._proc_width
        sh = max(1, int(h * sw / w))
        small = cv2.resize(frame, (sw, sh))
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
        result = self._detector.detect(image)
        return list(result.detections or []), sw, sh

    @staticmethod
    def _largest_center(detections: list[Any], width: int, height: int) -> tuple[float, float] | None:
        """Pure geometry: normalized center of the widest detection's bounding box."""
        if not detections:
            return None
        box = max(detections, key=lambda d: d.bounding_box.width).bounding_box
        cx = (box.origin_x + box.width / 2) / width
        cy = (box.origin_y + box.height / 2) / height
        return (cx, cy)


class FacePresenceDetector:
    """Emits a ``"face"`` :class:`Detection` while someone is in view.

    :class:`FaceLocator` by itself only answers "where is the face right now"
    for ``look_at_face``'s error signal; this adapter is what actually feeds
    the glance reflex through :class:`VisionWatch`. Requiring a few
    consecutive frames with a face filters out single-frame false positives;
    rate limiting beyond that is VisionWatch's per-kind cooldown plus
    :class:`~palmimo_companion_agent.core.reflexes.ReflexEngine`'s own refractory
    window, so a person who stays in frame draws an occasional glance rather
    than constant attention.
    """

    kind: ClassVar[DetectionKind] = DetectionKind.FACE

    def __init__(self, locator: FaceLocator, *, consecutive_hits: int = 3) -> None:
        self._locator = locator
        self._needed = consecutive_hits
        self._hits = 0

    def process(self, frame: Any, timestamp: float) -> Detection | None:
        if self._locator.locate(frame) is None:
            self._hits = 0
            return None
        self._hits += 1
        if self._hits < self._needed:
            return None
        self._hits = 0
        return Detection(summary="a face is in view", kind=self.kind)


# ----------------------------------------------------------------------
# VisionWatch -- registers on the SDK camera's background drain, debounces,
# yields Detections
# ----------------------------------------------------------------------


@runtime_checkable
class FrameCamera(Protocol):
    """The subset of :class:`~palmimo_sdk.io.camera.HeadCamera` :class:`VisionWatch` needs."""

    def add_consumer(self, fn: Callable[[Any, float], None]) -> None: ...


class VisionWatch:
    """Feeds the SDK camera's drained frames through a list of detectors, debounced.

    Registers :meth:`_on_frame` as a consumer of *camera* (the same
    :class:`~palmimo_sdk.io.camera.HeadCamera` instance
    :meth:`~palmimo_sdk.robot.Palmimo.connect` already opened and started
    draining -- this class does not own the camera's lifecycle, only adds
    itself as one more reader of its background drain). Each drained frame is
    run through every detector in order; a :class:`Detection` is enqueued only
    if *cooldown_seconds* have passed since the last one of the same ``kind``,
    so a held-out wave (or a lingering face) doesn't flood the queue.

    Args:
        camera: The shared frame source (``add_consumer``-capable).
        detectors: Detectors to run, in order, on every frame.
        cooldown_seconds: Minimum seconds between two enqueued detections of
            the same ``kind``.
    """

    def __init__(self, camera: FrameCamera, detectors: Iterable[Detector], *, cooldown_seconds: float = 8.0) -> None:
        self._camera = camera
        self._detectors: list[Detector] = list(detectors)
        self._cooldown = cooldown_seconds
        self._queue: asyncio.Queue[Detection | None] | None = None  # set in watch() (needs the running loop)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._last_emit: dict[DetectionKind, float] = {}
        self._registered = False
        # Single-slot hand-off from the camera's drain thread to _process_loop
        # (this class's own worker thread): _on_frame only ever writes here,
        # _process_loop only ever reads/clears it.
        self._frame_lock = threading.Lock()
        self._latest_frame: tuple[Any, float] | None = None
        self._frame_available = threading.Event()
        self._worker: threading.Thread | None = None
        self._worker_stop = threading.Event()

    async def watch(self) -> AsyncIterator[Detection]:
        """Yield debounced :class:`Detection`\\ s as the camera delivers frames.

        Registers the frame consumer and starts the detector worker thread on
        first call; safe to call more than once only in the sense that
        re-registration is skipped (a second concurrent ``watch()`` iterator
        would still share the same queue).
        """
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        if not self._registered:
            self._camera.add_consumer(self._on_frame)
            self._registered = True
            self._worker_stop.clear()
            self._worker = threading.Thread(target=self._process_loop, name="vision-watch-detectors", daemon=True)
            self._worker.start()
        while not self._closed:
            det = await self._queue.get()
            if self._closed or det is None:
                break
            yield det

    async def aclose(self) -> None:
        """Stop :meth:`watch`'s loop and the detector worker thread (does not touch the camera's own lifecycle)."""
        self._closed = True
        self._worker_stop.set()
        self._frame_available.set()  # wake the worker if it's blocked waiting for a frame
        if self._queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)
        if self._worker is not None:
            await asyncio.to_thread(self._worker.join, 2.0)

    def _on_frame(self, frame: Any, timestamp: float) -> None:
        """Camera drain-thread callback: stash the latest frame only, no detector work here.

        This runs on the SDK camera's own background drain thread, so it must
        stay cheap -- running MediaPipe inference here would stall frame
        draining and make ``camera.latest()`` return stale frames to every
        other consumer. The single slot is simply overwritten on each call;
        whichever frame :meth:`_process_loop` hasn't picked up yet by the next
        callback is silently dropped in favor of the newest one, so detection
        always runs on the most current frame rather than queuing up a
        backlog.
        """
        if self._closed:
            return
        with self._frame_lock:
            self._latest_frame = (frame, timestamp)
        self._frame_available.set()

    def _process_loop(self) -> None:
        """Worker-thread body: pull the latest frame and run detectors on it.

        Owns all MediaPipe/cv2 inference for this :class:`VisionWatch`,
        off the camera's drain thread (see :meth:`_on_frame`).
        """
        while not self._worker_stop.is_set():
            if not self._frame_available.wait(timeout=0.5):
                continue
            with self._frame_lock:
                item = self._latest_frame
                self._latest_frame = None
                self._frame_available.clear()
            if item is None:
                continue
            frame, timestamp = item
            self._run_detectors(frame, timestamp)

    def _run_detectors(self, frame: Any, timestamp: float) -> None:
        """Run every detector on *frame*, debounce per-kind, enqueue survivors."""
        for detector in self._detectors:
            try:
                detection = detector.process(frame, timestamp)
            except Exception:
                _log.exception("detector %s failed; continuing", getattr(detector, "kind", "?"))
                continue
            if detection is None:
                continue
            last = self._last_emit.get(detection.kind)
            if last is not None and timestamp - last < self._cooldown:
                continue
            self._last_emit[detection.kind] = timestamp
            loop, queue = self._loop, self._queue
            if loop is not None and queue is not None:
                loop.call_soon_threadsafe(queue.put_nowait, detection)


__all__ = ["Detection", "DetectionKind", "Detector", "FaceLocator", "FrameCamera", "VisionWatch", "WaveDetector"]
