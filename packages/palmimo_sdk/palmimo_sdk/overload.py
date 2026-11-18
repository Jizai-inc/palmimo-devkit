# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Leg overload guard — pure classifiers over ``ServoDriver.read_telemetry()``.

A leg servo that is held or blocked (a hand gripping the waving arm, a foot
wedged in a gap) draws current until the servo latches its own Overload HW error
and drops torque, which lets the body fall. :class:`OverloadGuard` flags that
strain a few samples early, and :class:`SoftReturnMonitor` decides when an arm
that was softened in response has settled back home.

Both classes only judge numbers they are handed. Reading the driver on a
schedule is :class:`~palmimo_sdk._leg_safety.LegSafety`'s job and acting on a
verdict is :class:`~palmimo_sdk.robot.Palmimo`'s (in
:meth:`~palmimo_sdk.robot.Palmimo.step`), which keeps this module free of I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from ._guard import Streak


# Raw Present_Current units (signed, compared by magnitude). The arm of a wave
# runs lighter than a weight-bearing leg, so its threshold is lower.
ARM_OVERLOAD_CURRENT = 700
LEG_OVERLOAD_CURRENT = 1200
# Samples in a row at or above the threshold before the guard trips.
OVERLOAD_CONSECUTIVE = 3
OVERLOAD_POLL_INTERVAL_S = 1.0 / 30.0
# Samples further apart than this are not consecutive: the streaks restart.
STREAK_MAX_GAP_S = 6 * OVERLOAD_POLL_INTERVAL_S

# Stall stage: a lower current counts as strain only while the axis is also this
# far from its goal. Provisional values, not yet checked against measured peaks
# and following lag on the robot; the stage is off unless a stall current is passed.
ARM_STALL_CURRENT = 450
LEG_STALL_CURRENT = 800
# About 22 degrees (4096 ticks per turn).
STALL_GAP_TICKS = 250

# Position_P_Gain an arm is held at while it eases back to neutral.
SOFT_RETURN_GAIN = 100
# Band around neutral (about 5 degrees) that counts as "home", wide enough for
# the sag a soft arm settles into under its own weight.
SOFT_RETURN_NEUTRAL_TICKS = 60
SOFT_RETURN_SETTLE_SAMPLES = 5

OverloadChannel = Literal["arm", "leg"]


@dataclass(frozen=True)
class OverloadTrip:
    """One overload verdict: which axis strained, and what the facade did about it.

    Attributes:
        motor (str): Axis that reached the streak first.
        current (int): Its raw signed current on the tripping sample.
        threshold (int): Magnitude the current was held at or above.
        samples (int): Consecutive samples at or above the threshold.
        motion (str): Motion that was running; filled in by the facade, ``""``
            straight out of :meth:`OverloadGuard.sample`.
        channel (str): ``"arm"`` for the waving arm's guard, ``"leg"`` otherwise.
        gap (int, optional): Ticks between the axis's position and its goal on the
            tripping sample when only the stall stage counted that sample, in which
            case *threshold* is the stall current; ``None`` when the current alone
            reached the threshold.
    """

    motor: str
    current: int
    threshold: int
    samples: int
    motion: str
    channel: OverloadChannel
    gap: int | None = None


class OverloadGuard:
    """Trips when an axis's current magnitude holds at or above a threshold.

    A per-axis streak gives the hysteresis: an axis must stay at or above
    *threshold* for *consecutive* samples in a row, so one noisy read cannot
    stop a healthy motion. A sample below the threshold zeroes that axis's streak.

    With *stall_current* set, a sample at or above that lower magnitude also
    counts when the axis is at least *stall_gap* ticks from its goal: current
    alone cannot tell a heavy pose from an axis that is caught and cannot move,
    but an axis that is not reaching its goal can. Both kinds of sample advance
    the same streak.

    Args:
        threshold (int): Current magnitude, in raw register units, that counts as strain.
        consecutive (int): Samples in a row needed to trip.
        channel (str): Label carried on the verdicts (``"arm"`` or ``"leg"``).
        stall_current (int, optional): Lower magnitude that counts while the axis is
            short of its goal; ``None`` turns the stall stage off.
        stall_gap (int): Distance from the goal, in ticks, that counts as short of it.

    Raises:
        ValueError: *threshold* is not positive, *consecutive* is below 1,
            *stall_current* is not between 0 and *threshold* (exclusive), or
            *stall_gap* is not positive.
    """

    def __init__(
        self,
        threshold: int,
        *,
        consecutive: int = OVERLOAD_CONSECUTIVE,
        channel: OverloadChannel = "leg",
        stall_current: int | None = None,
        stall_gap: int = STALL_GAP_TICKS,
    ) -> None:
        if threshold <= 0:
            raise ValueError("threshold must be positive (raw current units).")
        if consecutive < 1:
            raise ValueError("consecutive must be at least 1.")
        if stall_current is not None and not 0 < stall_current < threshold:
            raise ValueError("stall_current must be positive and below threshold (raw current units).")
        if stall_gap <= 0:
            raise ValueError("stall_gap must be positive (raw tick units).")
        self._threshold = threshold
        self._consecutive = consecutive
        self._channel: OverloadChannel = channel
        self._stall_current = stall_current
        self._stall_gap = stall_gap
        self._streaks = Streak()

    @property
    def threshold(self) -> int:
        """Current magnitude, in raw units, that counts as strain."""
        return self._threshold

    @property
    def strained(self) -> bool:
        """Whether any axis was at or above the threshold when last sampled (``False`` right after a trip)."""
        return self._streaks.active

    def sample(
        self,
        currents: Mapping[str, int],
        motors: Sequence[str],
        *,
        positions: Mapping[str, int] | None = None,
        goals: Mapping[str, int] | None = None,
    ) -> OverloadTrip | None:
        """Fold one sweep into the streaks and return a verdict if an axis trips.

        Only *motors* are judged. One missing from *currents* was not read this
        sweep, so its streak neither advances nor resets. A trip clears every
        streak, so the next trip needs a fresh run. When several axes reach the
        streak on the same sweep, the first in *motors* order is reported.

        The stall stage judges an axis only when both its position and its goal
        are known; without them, only *threshold* applies.

        Args:
            currents (Mapping[str, int]): Axis name -> raw signed current.
            motors (Sequence[str]): Axes to judge, in tie-break order.
            positions (Mapping[str, int], optional): Axis name -> present position, from the same sweep.
            goals (Mapping[str, int], optional): Axis name -> goal position the axis was last sent.
        """
        tripped: OverloadTrip | None = None
        for motor in motors:
            if motor not in currents:
                continue
            current = int(currents[motor])
            threshold = self._threshold
            gap = None
            if abs(current) < threshold:
                gap = self._stall_gap_of(motor, current, positions, goals)
                if gap is None:
                    self._streaks.drop(motor)
                    continue
                threshold = self._stall_current or threshold
            streak = self._streaks.advance(motor)
            if tripped is None and streak >= self._consecutive:
                tripped = OverloadTrip(motor, current, threshold, streak, "", self._channel, gap)
        if tripped is not None:
            self._streaks.clear()
        return tripped

    def _stall_gap_of(
        self,
        motor: str,
        current: int,
        positions: Mapping[str, int] | None,
        goals: Mapping[str, int] | None,
    ) -> int | None:
        """Distance from the goal when the stall stage counts this sample, else ``None``."""
        if self._stall_current is None or abs(current) < self._stall_current:
            return None
        if positions is None or goals is None or motor not in positions or motor not in goals:
            return None
        gap = abs(int(positions[motor]) - int(goals[motor]))
        return gap if gap >= self._stall_gap else None

    def forget(self, motors: Sequence[str]) -> None:
        """Drop the streaks of *motors*, which were unobserved for a while and may have changed since."""
        for motor in motors:
            self._streaks.drop(motor)

    def reset(self) -> None:
        """Forget every streak."""
        self._streaks.clear()


class SoftReturnMonitor:
    """Decides when a softened arm has eased back to its neutral pose.

    An axis missing from a sweep (set aside as unresponsive, say) resets the
    count, so the soft return then ends only by the caller's own decision.

    Position rather than current is the signal: at the soft gain a gripped arm
    draws little current too, but it stays far from neutral, so only a released
    arm settles inside the band.

    Args:
        goal (int): Neutral tick every watched axis is expected to reach.
        tolerance (int): Allowed distance from *goal*, in ticks.
        consecutive (int): Sweeps in a row the whole arm must stay inside the band.
    """

    def __init__(
        self,
        goal: int,
        *,
        tolerance: int = SOFT_RETURN_NEUTRAL_TICKS,
        consecutive: int = SOFT_RETURN_SETTLE_SAMPLES,
    ) -> None:
        if tolerance < 0:
            raise ValueError("tolerance must be non-negative (raw tick units).")
        if consecutive < 1:
            raise ValueError("consecutive must be at least 1.")
        self._goal = goal
        self._tolerance = tolerance
        self._consecutive = consecutive
        self._streak = 0

    def sample(self, positions: Mapping[str, int], motors: Sequence[str]) -> bool:
        """Fold one sweep in; ``True`` once the whole arm has settled at neutral.

        A sweep missing any of *motors* cannot confirm the arm is home, so it
        zeroes the streak rather than releasing a gain it cannot justify.
        """
        home = bool(motors) and all(
            motor in positions and abs(int(positions[motor]) - self._goal) <= self._tolerance for motor in motors
        )
        self._streak = self._streak + 1 if home else 0
        return self._streak >= self._consecutive

    def reset(self) -> None:
        """Forget the streak."""
        self._streak = 0
