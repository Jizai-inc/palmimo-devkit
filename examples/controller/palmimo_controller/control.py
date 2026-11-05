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
