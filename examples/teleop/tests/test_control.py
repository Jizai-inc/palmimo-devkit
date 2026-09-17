"""Behavior of `palmimo_teleop.control.resolve_motion` and `clamp_neck`."""

import pytest

from palmimo_teleop.control import clamp_neck, resolve_motion


@pytest.mark.parametrize(
    ("move", "rotate", "expected"),
    [
        ("forward", 0, "forward"),
        ("backward", 0, "backward"),
        ("strafe_left", 0, "strafe_left"),
        ("strafe_right", 0, "strafe_right"),
        (None, 0, None),
    ],
)
def test_resolve_motion_passes_through_move_when_not_rotating(
    move: str | None, rotate: int, expected: str | None
) -> None:
    # Without this, a pilot holding just a direction button would get no
    # motion at all -- the whole point of translation input.
    assert resolve_motion(move, rotate) == expected


@pytest.mark.parametrize("move", ["forward", "backward", "strafe_left", "strafe_right", None])
def test_resolve_motion_rotate_overrides_any_move(move: str | None) -> None:
    # Without this, holding a direction button and a rotate button together
    # would blend into an undefined gait instead of turning in place --
    # MotionEngine has no combined motion to fall back on.
    assert resolve_motion(move, 1) == "rotate_right"
    assert resolve_motion(move, -1) == "rotate_left"


@pytest.mark.parametrize("rotate", [-1, 0, 1])
@pytest.mark.parametrize("move", ["forward", "backward", "strafe_left", "strafe_right", None])
def test_resolve_motion_gesture_overrides_rotate_and_move(move: str | None, rotate: int) -> None:
    # Without this, holding a gesture button while also steering would blend
    # into an undefined motion instead of the gesture taking over --
    # MotionEngine has no motion that mixes a gesture with a gait.
    assert resolve_motion(move, rotate, "wave") == "wave"
    assert resolve_motion(move, rotate, "dance") == "dance"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, 0.0),
        (1.0, 1.0),
        (-1.0, -1.0),
        (1.5, 1.0),
        (-1.5, -1.0),
        (float("inf"), 0.0),
        (float("-inf"), 0.0),
        (float("nan"), 0.0),
    ],
)
def test_clamp_neck_bounds_to_normalized_range(value: float, expected: float) -> None:
    # Without this, a malformed/malicious pilot frame -- including a NaN or
    # an `Infinity` literal, which Python's `json` module accepts unlike
    # standard JSON -- could send a neck value that skips [-1, 1] clamping
    # (NaN passes both `min`/`max` comparisons, reaching `Palmimo.look` at
    # full, un-clamped deflection).
    assert clamp_neck(value) == expected
