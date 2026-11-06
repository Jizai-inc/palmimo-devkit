"""Pure resolution from a pilot's move/rotate pair to a `Palmimo.set_motion` name.

Kept separate from `session.py` so the "rotate wins over translation" rule is
one small, exhaustively testable function rather than folded into the
session's deadman bookkeeping.
"""

from __future__ import annotations

import math


#: Move names the client is allowed to send. Anything else is rejected by the
#: server's input validation before it ever reaches this module.
ALLOWED_MOVES: frozenset[str] = frozenset({"forward", "backward", "strafe_left", "strafe_right"})

#: Rotate values the client is allowed to send.
ALLOWED_ROTATES: frozenset[int] = frozenset({-1, 0, 1})

#: Gesture names the client is allowed to send. Anything else is rejected by
#: the server's input validation before it ever reaches this module.
ALLOWED_GESTURES: frozenset[str] = frozenset({"wave", "dance"})

#: Playback speed multipliers the pilot may select, applied to walking and
#: rotating, and (up to `MAX_GESTURE_SPEED`) to wave and dance. A closed set
#: rather than a free-form number: each entry is a speed someone chose to
#: offer, and anything else is rejected by the server's input validation like
#: an unknown gait name.
ALLOWED_SPEEDS: tuple[float, ...] = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0)

#: Highest multiplier the wave and dance are played at; a higher selection
#: speeds up only walking and rotating. Gestures have been run on hardware up
#: to this speed and no further.
MAX_GESTURE_SPEED: float = 3.0

#: Fraction of the startup step height (swing-leg lift) a gait multiplier
#: lifts the legs by; multipliers not listed keep the full height. At 4x a
#: full-height lift asks the lift joints for more speed than the servos can
#: comfortably give: measured on a robot on a stand, lowering it to 0.6 of
#: 30 mm cut the peak lift-joint speed from about 300 to about 240 deg/s and
#: the peak leg current with it, while the stride was kept.
STEP_HEIGHT_SCALE: dict[float, float] = {4.0: 0.6}

#: The multiplier a session starts at and falls back to once no pilot is
#: driving -- the SDK's own tuned speed.
DEFAULT_SPEED: float = 1.0

#: Ceiling on the gait phase increment a multiplier may produce, shared with
#: the `--gait-speed` option's own maximum so a multiplied gait never runs
#: faster than the fastest one the CLI accepts.
MAX_GAIT_SPEED: float = 0.05

#: Motion names `resolve_motion` can return that are a walking or rotating
#: gait (as opposed to a gesture) -- the motions whose leg servo profile the
#: robot loop shortens with the speed multiplier.
GAIT_MOTIONS: frozenset[str] = ALLOWED_MOVES | {"rotate_left", "rotate_right"}

#: The 18 leg servos, whose Profile_Velocity the robot loop retunes; the neck
#: is left alone.
LEG_MOTORS: tuple[str, ...] = tuple(f"leg_{leg}_{axis}" for leg in range(1, 7) for axis in ("yaw", "pitch1", "pitch2"))

#: `Palmimo` neck axes are normalized [-1, 1]; a pilot input past that range
#: (a malicious or buggy client) is clamped rather than rejected -- there is
#: no unsafe neck position, unlike an unknown gait name.
NECK_MIN: float = -1.0
NECK_MAX: float = 1.0


def resolve_motion(move: str | None, rotate: int, gesture: str | None = None) -> str | None:
    """Resolve a pilot's move/rotate/gesture triple to a `Palmimo.set_motion` name, or `None` to stop.

    Priority is gesture > rotate > move: an active played gesture (wave/dance)
    overrides steering the same way rotate overrides translation, since
    `MotionEngine` has no motion that mixes a gesture with a gait. `move` is
    passed through unchanged when both `rotate` is 0 and `gesture` is `None`.

    Args:
        move (str, optional): One of `ALLOWED_MOVES`, or `None` for no translation.
        rotate (int): -1 (left), 0 (none), or 1 (right).
        gesture (str, optional): One of `ALLOWED_GESTURES`, or `None`.

    Returns:
        str | None: A `Palmimo.set_motion` name, or `None` when no axis is active.
    """
    if gesture is not None:
        return gesture
    if rotate == 1:
        return "rotate_right"
    if rotate == -1:
        return "rotate_left"
    return move


def clamp_neck(value: float) -> float:
    """Clamp a neck axis value to the `Palmimo.look` normalized range `[-1, 1]`.

    A non-finite *value* (NaN, or the `Infinity`/`-Infinity` literals
    Python's `json` module accepts, unlike standard JSON) collapses to
    `0.0` rather than through `min`/`max` -- NaN would otherwise pass either
    comparison and reach `Palmimo.look` unclamped, and there is no
    "clamped" version of infinity that is any safer than centered.
    """
    if not math.isfinite(value):
        return 0.0
    return max(NECK_MIN, min(NECK_MAX, value))
