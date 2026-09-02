"""Tests for :mod:`palmimo_companion_agent.core.vision`.

No mediapipe / cv2 / real model / network involved anywhere: the reversal-wave
judge and the face-locator's box-picking geometry are pure Python and tested
directly; :class:`WaveDetector.process` is exercised via its
``_update``/``_locate_palms`` split with the model-backed half swapped for a
hand-picked list of palm positions (constructed with ``object.__new__`` so
``__init__`` -- which imports mediapipe -- never runs).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from palmimo_companion_agent.core.vision import (
    Detection,
    DetectionKind,
    FaceLocator,
    FacePresenceDetector,
    VisionWatch,
    WaveDetector,
    _ReversalWaveJudge,
)


# ----------------------------------------------------------------------
# _ReversalWaveJudge -- pure Python, no mediapipe
# ----------------------------------------------------------------------


def test_reversal_wave_judge_fires_after_enough_reversals() -> None:
    judge = _ReversalWaveJudge(move_threshold=0.05, wave_count_threshold=2, clear_count_threshold=5)

    xs = [0.5, 0.6, 0.5, 0.6, 0.5]  # right, left, right, left -> 3 reversals
    fired = [judge.update(x) for x in xs]

    assert any(fired)


def test_reversal_wave_judge_does_not_fire_on_a_steady_hand() -> None:
    judge = _ReversalWaveJudge(move_threshold=0.05, wave_count_threshold=2, clear_count_threshold=5)

    fired = [judge.update(0.5) for _ in range(10)]

    assert not any(fired)


def test_reversal_wave_judge_ignores_moves_below_the_threshold() -> None:
    judge = _ReversalWaveJudge(move_threshold=0.1, wave_count_threshold=2, clear_count_threshold=5)

    xs = [0.5, 0.52, 0.5, 0.52, 0.5]  # tiny jitter, all below 0.1
    fired = [judge.update(x) for x in xs]

    assert not any(fired)


def test_reversal_wave_judge_resets_history_after_losing_the_hand() -> None:
    judge = _ReversalWaveJudge(move_threshold=0.05, wave_count_threshold=2, clear_count_threshold=3)
    judge.update(0.5)
    judge.update(0.6)  # one reversal-worth of history built up

    for _ in range(3):
        judge.update(None)  # lost long enough to clear

    assert judge.wave_count == 0
    assert list(judge.x_history) == []


# ----------------------------------------------------------------------
# WaveDetector -- __init__ bypassed (no mediapipe import), _update tested directly
# ----------------------------------------------------------------------


def _make_wave_detector() -> WaveDetector:
    detector = object.__new__(WaveDetector)
    detector._hands = []
    return detector


def test_wave_detector_reports_a_wave_on_the_right_side() -> None:
    detector = _make_wave_detector()

    result: Detection | None = None
    for x in [0.7, 0.8, 0.7, 0.8, 0.7]:
        result = detector._update([(x, 0.5)]) or result

    assert result is not None
    assert result.kind == DetectionKind.WAVE
    assert "right" in result.summary


def test_wave_detector_reports_a_wave_on_the_left_side() -> None:
    detector = _make_wave_detector()

    result: Detection | None = None
    for x in [0.2, 0.3, 0.2, 0.3, 0.2]:
        result = detector._update([(x, 0.5)]) or result

    assert result is not None
    assert "left" in result.summary


def test_wave_detector_returns_none_for_a_steady_hand() -> None:
    detector = _make_wave_detector()

    results = [detector._update([(0.5, 0.5)]) for _ in range(10)]

    assert all(r is None for r in results)


def test_wave_detector_returns_none_for_no_hands() -> None:
    detector = _make_wave_detector()

    assert detector._update([]) is None


def test_wave_detector_drops_a_track_after_it_is_lost_long_enough() -> None:
    detector = _make_wave_detector()
    detector._update([(0.5, 0.5)])
    assert len(detector._hands) == 1

    for _ in range(10):  # well past _CLEAR_COUNT
        detector._update([])

    assert detector._hands == []


def test_wave_detector_tracks_two_hands_independently() -> None:
    detector = _make_wave_detector()

    result: Detection | None = None
    for _ in range(5):
        left = 0.2 if _ % 2 == 0 else 0.3
        right = 0.7 if _ % 2 == 0 else 0.8
        result = detector._update([(left, 0.5), (right, 0.5)]) or result

    assert len(detector._hands) == 2
    assert result is not None  # at least one side waved


# ----------------------------------------------------------------------
# FaceLocator._largest_center -- pure geometry, no mediapipe
# ----------------------------------------------------------------------


class _FakeBox:
    def __init__(self, origin_x: float, origin_y: float, width: float, height: float) -> None:
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.width = width
        self.height = height


class _FakeDetection:
    def __init__(self, box: _FakeBox) -> None:
        self.bounding_box = box


def test_face_locator_largest_center_returns_none_for_no_detections() -> None:
    assert FaceLocator._largest_center([], 640, 480) is None


def test_face_locator_largest_center_computes_the_normalized_center() -> None:
    detections = [_FakeDetection(_FakeBox(origin_x=100, origin_y=50, width=200, height=100))]

    cx, cy = FaceLocator._largest_center(detections, width=640, height=480)

    assert cx == pytest.approx((100 + 100) / 640)
    assert cy == pytest.approx((50 + 50) / 480)


def test_face_locator_largest_center_picks_the_widest_box() -> None:
    small = _FakeDetection(_FakeBox(origin_x=0, origin_y=0, width=50, height=50))
    big = _FakeDetection(_FakeBox(origin_x=300, origin_y=300, width=200, height=200))

    cx, cy = FaceLocator._largest_center([small, big], width=640, height=480)

    assert cx == pytest.approx((300 + 100) / 640)
    assert cy == pytest.approx((300 + 100) / 480)


# ----------------------------------------------------------------------
# VisionWatch -- consumer flow + cooldown, with a fake camera
# ----------------------------------------------------------------------


class FakeCamera:
    """Stands in for HeadCamera: just remembers the registered consumer."""

    def __init__(self) -> None:
        self.consumer: Any = None

    def add_consumer(self, fn: Any) -> None:
        self.consumer = fn


async def _wait_until(predicate: Any, *, timeout: float = 2.0, interval: float = 0.005) -> None:
    """Poll *predicate* until it is truthy, or fail after *timeout* seconds.

    Detector processing now happens on a dedicated worker thread (see
    :meth:`VisionWatch._process_loop`), so tests can no longer assume a fixed
    sleep is enough for a frame handed to ``_on_frame`` to have been
    processed and enqueued -- poll instead of guessing a sleep duration.
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


class ScriptedDetector:
    """Returns the next scripted Detection (or None) on each call, in order."""

    kind = DetectionKind.WAVE

    def __init__(self, results: list[Detection | None]) -> None:
        self._results = list(results)

    def process(self, frame: Any, timestamp: float) -> Detection | None:
        return self._results.pop(0) if self._results else None


async def test_vision_watch_yields_a_detection_from_the_camera_consumer() -> None:
    camera = FakeCamera()
    detection = Detection(summary="someone is waving", kind=DetectionKind.WAVE)
    detector = ScriptedDetector([detection])
    watch = VisionWatch(camera, [detector], cooldown_seconds=8.0)

    received: list[Detection] = []

    async def consume() -> None:
        async for det in watch.watch():
            received.append(det)

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0)  # let watch() register the consumer
    assert camera.consumer is not None
    camera.consumer("frame-1", 0.0)
    await _wait_until(lambda: len(received) >= 1)

    await watch.aclose()
    await task

    assert received == [detection]


async def test_vision_watch_debounces_within_the_cooldown_window() -> None:
    camera = FakeCamera()
    det_a = Detection(summary="a", kind=DetectionKind.WAVE)
    det_b = Detection(summary="b", kind=DetectionKind.WAVE)
    detector = ScriptedDetector([det_a, det_b])
    watch = VisionWatch(camera, [detector], cooldown_seconds=8.0)

    received: list[Detection] = []

    async def consume() -> None:
        async for det in watch.watch():
            received.append(det)

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0)
    camera.consumer("frame-1", 0.0)  # t=0: emitted
    await _wait_until(lambda: len(received) >= 1)  # let the worker drain frame-1 before frame-2 lands
    camera.consumer("frame-2", 1.0)  # t=1, within 8s cooldown of the same kind: dropped
    await asyncio.sleep(0.05)  # give the (already-warmed-up) worker a chance to process and drop it

    await watch.aclose()
    await task

    assert received == [det_a]


async def test_vision_watch_emits_again_after_the_cooldown_elapses() -> None:
    camera = FakeCamera()
    det_a = Detection(summary="a", kind=DetectionKind.WAVE)
    det_b = Detection(summary="b", kind=DetectionKind.WAVE)
    detector = ScriptedDetector([det_a, det_b])
    watch = VisionWatch(camera, [detector], cooldown_seconds=8.0)

    received: list[Detection] = []

    async def consume() -> None:
        async for det in watch.watch():
            received.append(det)

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0)
    camera.consumer("frame-1", 0.0)
    await _wait_until(lambda: len(received) >= 1)  # let the worker drain frame-1 before frame-2 lands
    camera.consumer("frame-2", 9.0)  # past the 8s cooldown: emitted
    await _wait_until(lambda: len(received) >= 2)

    await watch.aclose()
    await task

    assert received == [det_a, det_b]


async def test_vision_watch_swallows_a_detector_exception() -> None:
    camera = FakeCamera()

    class BrokenDetector:
        # Arbitrary valid kind: only .process() (which always raises) matters
        # for this test, the label itself is never asserted.
        kind = DetectionKind.WAVE

        def process(self, frame: Any, timestamp: float) -> Detection | None:
            raise RuntimeError("boom")

    good = Detection(summary="ok", kind=DetectionKind.WAVE)
    watch = VisionWatch(camera, [BrokenDetector(), ScriptedDetector([good])])

    received: list[Detection] = []

    async def consume() -> None:
        async for det in watch.watch():
            received.append(det)

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0)
    camera.consumer("frame-1", 0.0)  # broken detector raises, good detector still runs
    await _wait_until(lambda: len(received) >= 1)

    await watch.aclose()
    await task

    assert received == [good]


async def test_on_frame_does_not_block_the_calling_thread() -> None:
    """_on_frame must be a cheap "store the latest frame" callback, not run detectors inline.

    Regression test: _on_frame used to call detector.process() (MediaPipe
    inference) synchronously on the camera's drain thread. A slow detector
    would then stall frame draining, making camera.latest() return stale
    frames elsewhere. Detector work must happen on VisionWatch's own worker
    thread instead.
    """
    camera = FakeCamera()

    class SlowDetector:
        # Arbitrary valid kind, not asserted -- see BrokenDetector above.
        kind = DetectionKind.WAVE

        def __init__(self) -> None:
            self.calls = 0

        def process(self, frame: Any, timestamp: float) -> Detection | None:
            self.calls += 1
            time.sleep(0.3)
            return None

    detector = SlowDetector()
    watch = VisionWatch(camera, [detector])

    async def consume() -> None:
        async for _ in watch.watch():
            pass

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0)  # let watch() register the consumer

    start = time.monotonic()
    camera.consumer("frame-1", 0.0)  # must return immediately, not block for the detector's 0.3s
    elapsed = time.monotonic() - start

    assert elapsed < 0.1

    await _wait_until(lambda: detector.calls >= 1)
    await watch.aclose()
    await task


class _ScriptedLocator:
    """Stands in for FaceLocator: pops scripted (cx, cy) | None results."""

    def __init__(self, results: list[tuple[float, float] | None]) -> None:
        self._results = list(results)

    def locate(self, frame: object) -> tuple[float, float] | None:
        return self._results.pop(0) if self._results else None


def test_face_presence_detector_requires_consecutive_hits() -> None:
    detector = FacePresenceDetector(_ScriptedLocator([(0.5, 0.5), (0.5, 0.5), None, (0.5, 0.5)]), consecutive_hits=3)
    assert detector.process(frame=None, timestamp=0.0) is None
    assert detector.process(frame=None, timestamp=0.1) is None
    assert detector.process(frame=None, timestamp=0.2) is None  # miss resets the streak
    assert detector.process(frame=None, timestamp=0.3) is None  # streak restarts at 1


def test_face_presence_detector_emits_after_streak_then_rearms() -> None:
    hits: list[tuple[float, float] | None] = [(0.5, 0.5)] * 6
    detector = FacePresenceDetector(_ScriptedLocator(hits), consecutive_hits=3)
    results = [detector.process(frame=None, timestamp=i * 0.1) for i in range(6)]
    detections = [r for r in results if r is not None]
    assert len(detections) == 2  # frames 3 and 6: emits, then re-arms for another full streak
    assert all(d.kind == DetectionKind.FACE for d in detections)
