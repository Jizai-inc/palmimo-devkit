"""Wave and face detection on head-camera frames, for the AI mode's reflexes.

Adapted from the companion agent's `core/vision.py` (an example is
self-contained, so it carries its own copy): MediaPipe HandLandmarker plus a
direction-reversal judge for "someone is waving" (`WaveDetector`), and
MediaPipe FaceDetector for "where is the largest face" (`FaceLocator`). The
thresholds are the companion's, tuned on real hardware.

`mediapipe` and `cv2` are imported inside the constructors, so importing this
module never needs them; only building a real detector does. Each detector is
split into a MediaPipe call and a pure-Python step, so tests exercise the
judging and geometry without a model.

Both models are downloaded on first use to the same paths the companion
uses, so a robot that already ran the companion does not download them again.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_HAND_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/latest/hand_landmarker.task"
)
DEFAULT_HAND_MODEL = str(Path.home() / "hand_landmarker.task")

_FACE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_detector/"
    "blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
)
DEFAULT_FACE_MODEL = str(Path.home() / "blaze_face_short_range.tflite")

#: Palm-center landmarks (wrist + the four finger MCPs).
_PALM_LANDMARKS = (0, 5, 9, 13, 17)

#: Frame-to-frame palm x change (fraction of frame width) that counts as a swing.
MOVE_THRESHOLD = 0.025
#: Direction reversals needed to call it a wave.
WAVE_COUNT = 2
#: Consecutive frames without a hand before a track's history resets and the track is dropped.
CLEAR_COUNT = 5
#: Width frames are downscaled to before hand inference.
_HAND_PROC_WIDTH = 320
#: Width frames are downscaled to before face inference.
_FACE_PROC_WIDTH = 640
#: Largest normalized distance at which this frame's palm is matched to last
#: frame's track. HandLandmarker gives no persistent track id, and its
#: Left/Right label can swap between frames, so proximity is what correlates them.
_MATCH_MAX_DIST = 0.35


def _ensure_model(url: str, path: str, timeout: float) -> None:
    if Path(path).exists():
        return
    from palmimo_sdk.download import download_atomic

    download_atomic(url, path, timeout=timeout)


class ReversalWaveJudge:
    """Per-hand wave judge: counts direction reversals of the palm's x across frames."""

    def __init__(
        self,
        move_threshold: float = MOVE_THRESHOLD,
        wave_count_threshold: int = WAVE_COUNT,
        clear_count_threshold: int = CLEAR_COUNT,
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
        """Feed one frame's palm-center x (`None`: no hand this frame); `True` on a wave."""
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
    judge: ReversalWaveJudge
    x: float
    y: float
    lost_frames: int = 0


class WaveDetector:
    """Detects a waving hand: MediaPipe HandLandmarker, then a `ReversalWaveJudge` per hand.

    Call `process()` from one thread only; it keeps per-hand tracking state.
    """

    def __init__(self, model_path: str = DEFAULT_HAND_MODEL) -> None:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python.vision import HandLandmarker, HandLandmarkerOptions, RunningMode

        _ensure_model(_HAND_MODEL_URL, model_path, timeout=30.0)
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

    def process(self, frame: Any, timestamp: float) -> bool:
        """Feed one frame; `True` when a hand just completed a wave."""
        return self._update(self._locate_palms(frame, timestamp))

    def _locate_palms(self, frame: Any, timestamp: float) -> list[tuple[float, float]]:
        cv2 = self._cv2
        if self._proc_h is None:
            self._proc_h = max(1, round(frame.shape[0] * _HAND_PROC_WIDTH / frame.shape[1]))
        small = cv2.resize(frame, (_HAND_PROC_WIDTH, self._proc_h))
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        # VIDEO mode needs a strictly increasing millisecond timestamp.
        ts = max(self._ts_ms + 1, int(timestamp * 1000))
        self._ts_ms = ts
        result = self._landmarker.detect_for_video(image, ts)
        palms = []
        for landmarks in result.hand_landmarks or []:
            px = sum(landmarks[k].x for k in _PALM_LANDMARKS) / len(_PALM_LANDMARKS)
            py = sum(landmarks[k].y for k in _PALM_LANDMARKS) / len(_PALM_LANDMARKS)
            palms.append((px, py))
        return palms

    def _match_track(self, px: float, py: float, matched: set[int]) -> int | None:
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

    def _update(self, palms: Iterable[tuple[float, float]]) -> bool:
        """Match this frame's palms to tracks and judge each; `True` if any hand waved."""
        waved = False
        matched: set[int] = set()
        for px, py in palms:
            idx = self._match_track(px, py, matched)
            if idx is None:
                self._hands.append(_HandTrack(ReversalWaveJudge(), px, py))
                idx = len(self._hands) - 1
            track = self._hands[idx]
            track.x, track.y = px, py
            track.lost_frames = 0
            matched.add(idx)
            if track.judge.update(px):
                waved = True
        for idx, track in enumerate(self._hands):
            if idx not in matched:
                track.judge.update(None)
                track.lost_frames += 1
        self._hands = [t for t in self._hands if t.lost_frames < CLEAR_COUNT]
        return waved


class FaceLocator:
    """Finds the largest face's normalized center `(cx, cy)` in a frame."""

    def __init__(self, model_path: str = DEFAULT_FACE_MODEL, *, min_confidence: float = 0.4) -> None:
        import cv2
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision

        _ensure_model(_FACE_MODEL_URL, model_path, timeout=15.0)
        self._cv2 = cv2
        # MediaPipe detectors are not safe for concurrent detect calls.
        self._lock = threading.Lock()
        self._detector = vision.FaceDetector.create_from_options(
            vision.FaceDetectorOptions(
                base_options=python.BaseOptions(model_asset_path=model_path),
                running_mode=vision.RunningMode.IMAGE,
                min_detection_confidence=min_confidence,
            )
        )

    def locate(self, frame: Any) -> tuple[float, float] | None:
        """The largest face's normalized `(cx, cy)`, or `None` when no face is in view."""
        with self._lock:
            detections, width, height = self._detect(frame)
        return largest_center(detections, width, height)

    def _detect(self, frame: Any) -> tuple[list[Any], int, int]:
        import mediapipe as mp

        cv2 = self._cv2
        h, w = frame.shape[:2]
        sw = _FACE_PROC_WIDTH
        sh = max(1, int(h * sw / w))
        small = cv2.resize(frame, (sw, sh))
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
        result = self._detector.detect(image)
        return list(result.detections or []), sw, sh


def largest_center(detections: list[Any], width: int, height: int) -> tuple[float, float] | None:
    """Normalized center of the widest detection's bounding box, or `None` with no detections."""
    if not detections:
        return None
    box = max(detections, key=lambda d: d.bounding_box.width).bounding_box
    return ((box.origin_x + box.width / 2) / width, (box.origin_y + box.height / 2) / height)


__all__ = [
    "DEFAULT_FACE_MODEL",
    "DEFAULT_HAND_MODEL",
    "FaceLocator",
    "ReversalWaveJudge",
    "WaveDetector",
    "largest_center",
]
