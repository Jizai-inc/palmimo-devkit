"""Pure parts of `palmimo_controller.ai.vision`: the wave judge, hand tracking, and face geometry.

No MediaPipe model is loaded: `WaveDetector` is built with `object.__new__`
and fed palm positions directly.
"""

from dataclasses import dataclass

from palmimo_controller.ai.vision import (
    CLEAR_COUNT,
    MOVE_THRESHOLD,
    FaceBox,
    ReversalWaveJudge,
    WaveDetector,
    largest_center,
    largest_face,
)


SWING = MOVE_THRESHOLD * 2


def _swings(judge: ReversalWaveJudge, count: int) -> list[bool]:
    xs = [0.5 + (SWING if i % 2 else 0.0) for i in range(count)]
    return [judge.update(x) for x in xs]


def test_judge_calls_a_wave_after_two_reversals() -> None:
    assert _swings(ReversalWaveJudge(), 4) == [False, False, False, True]


def test_judge_ignores_small_jitter() -> None:
    judge = ReversalWaveJudge()
    assert not any(judge.update(0.5 + (MOVE_THRESHOLD / 2 if i % 2 else 0.0)) for i in range(10))


def test_judge_forgets_a_hand_that_left_the_frame() -> None:
    judge = ReversalWaveJudge()
    _swings(judge, 3)
    for _ in range(CLEAR_COUNT):
        judge.update(None)
    assert _swings(judge, 3) == [False, False, False]


def _detector() -> WaveDetector:
    detector = object.__new__(WaveDetector)
    detector._hands = []
    return detector


def test_detector_tracks_two_hands_separately() -> None:
    # A still hand next to a waving one must not break the wave up.
    detector = _detector()
    results = [detector._update([(0.2, 0.5), (0.7 + (SWING if i % 2 else 0.0), 0.5)]) for i in range(4)]
    assert results == [False, False, False, True]


@dataclass
class _Box:
    origin_x: float
    origin_y: float
    width: float
    height: float


@dataclass
class _Detection:
    bounding_box: _Box


def test_largest_center_picks_the_widest_face() -> None:
    small = _Detection(_Box(0, 0, 10, 10))
    big = _Detection(_Box(100, 50, 40, 40))
    assert largest_center([small, big], 200, 100) == (0.6, 0.7)


def test_largest_center_is_none_without_faces() -> None:
    assert largest_center([], 200, 100) is None


def test_largest_face_reports_the_widest_face_center_and_width_fraction() -> None:
    small = _Detection(_Box(0, 0, 10, 10))
    big = _Detection(_Box(100, 50, 40, 40))
    assert largest_face([small, big], 200, 100) == FaceBox(center=(0.6, 0.7), size=0.2)


def test_largest_face_is_none_without_faces() -> None:
    assert largest_face([], 200, 100) is None
