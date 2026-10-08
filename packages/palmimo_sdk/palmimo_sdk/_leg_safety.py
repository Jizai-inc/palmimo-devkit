# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Leg telemetry sweep feeding the arm overload, leg overload and rail undervoltage guards."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace

from ._guard import RestingReport, TripLog
from .engine import NECK_GESTURES, Motion, MotionEngine
from .io import ServoDriver
from .kinematics import LEG_MOTORS, leg_motors
from .overload import (
    OVERLOAD_POLL_INTERVAL_S,
    STREAK_MAX_GAP_S,
    OverloadGuard,
    OverloadTrip,
    SoftReturnMonitor,
)
from .rail import RailGuard, RailTrip
from .telemetry import TelemetryPoller


# Motions a soft-returning arm may coexist with; anything else ends the soft return.
_SOFT_RETURN_MOTIONS = (Motion.IDLE, *NECK_GESTURES)
_TRIP_HISTORY = 64


class LegSafety:
    """Reads leg telemetry and judges it against the overload and rail guards.

    The owner reacts to the verdict; this class never touches the motion engine.

    Args:
        arm_overload_current (int, optional): Threshold for a waving arm axis; ``None`` disables that guard.
        leg_overload_current (int, optional): Threshold for every other leg axis; ``None`` disables it.
        rail_undervoltage_v (float, optional): Rail voltage floor; ``None`` disables the guard.
        clock (Callable[[], float]): Monotonic time source for poll scheduling.
        logger (logging.Logger): Logger that receives the guards' records.
    """

    def __init__(
        self,
        *,
        arm_overload_current: int | None,
        leg_overload_current: int | None,
        rail_undervoltage_v: float | None,
        clock: Callable[[], float],
        logger: logging.Logger,
    ) -> None:
        self._arm_overload = (
            None if arm_overload_current is None else OverloadGuard(arm_overload_current, channel="arm")
        )
        self._leg_overload = (
            None if leg_overload_current is None else OverloadGuard(leg_overload_current, channel="leg")
        )
        self._rail = None if rail_undervoltage_v is None else RailGuard(rail_undervoltage_v)
        self._clock = clock
        self._logger = logger
        self._poller = TelemetryPoller(
            LEG_MOTORS, OVERLOAD_POLL_INTERVAL_S, guard_name="leg telemetry guard", logger=logger
        )
        # (motion, arm legs) the guards' streaks were counted under.
        self._scope: tuple[Motion, tuple[int, ...]] | None = None
        # When leg telemetry was last read successfully, or None before the first read.
        self._last_sample: float | None = None
        self.overload_trips: TripLog[OverloadTrip] = TripLog(_TRIP_HISTORY)
        self.rail_trips: TripLog[RailTrip] = TripLog(_TRIP_HISTORY)
        # A leg trip with no leg motion running has been recorded and the
        # strain has not cleared since; holds back the repeats.
        self._resting_strain = RestingReport()
        # Same hold-back, for a rail sag with no leg motion running.
        self._resting_sag = RestingReport()
        # Arm axes held at SOFT_RETURN_GAIN after an arm trip, or None.
        self._soft_return: tuple[str, ...] | None = None
        self._soft_return_monitor = SoftReturnMonitor(MotionEngine.NEUTRAL)

    def soft_return_under(self, motion: Motion) -> tuple[str, ...] | None:
        """Arm axes to hold at ``SOFT_RETURN_GAIN`` while *motion* runs.

        Args:
            motion (Motion): The motion currently running.

        Returns:
            tuple[str, ...] | None: The soft-returning arm axes, or ``None`` when there is no
                soft return or *motion* is one it cannot coexist with.
        """
        return self._soft_return if motion in _SOFT_RETURN_MOTIONS else None

    def poll(self, driver: ServoDriver | None, motion: Motion, arm_legs: tuple[int, ...]) -> bool:
        """Sample leg telemetry at most every ``OVERLOAD_POLL_INTERVAL_S`` and judge it.

        Args:
            driver (ServoDriver, optional): Connected driver to read from; ``None`` skips the poll.
            motion (Motion): The motion currently running.
            arm_legs (tuple[int, ...]): Legs acting as the raised arm under *motion*.

        Returns:
            bool: ``True`` if a guard tripped and the motion must stop.
        """
        if driver is None or self._poller.disabled:
            return False
        if self._arm_overload is None and self._leg_overload is None and self._rail is None:
            return False
        if self._soft_return is not None and motion not in _SOFT_RETURN_MOTIONS:
            self.end_soft_return()
        scope = (motion, arm_legs)
        if scope != self._scope:
            for guard in (self._arm_overload, self._leg_overload):
                if guard is not None:
                    guard.reset()
            self._scope = scope
        now = self._clock()
        if not self._poller.due(now):
            return False
        telemetry = self._poller.read(driver, now, "leg telemetry read failed, keeping the current streaks")
        if telemetry is None:
            return False
        if self._last_sample is not None and now - self._last_sample >= STREAK_MAX_GAP_S:
            for stale in (self._arm_overload, self._leg_overload, self._rail):
                if stale is not None:
                    stale.reset()
        self._last_sample = now
        arm = leg_motors(arm_legs)
        if self._soft_return is not None and self._soft_return_monitor.sample(telemetry.position, self._soft_return):
            self.end_soft_return()
        # IDLE and the neck gestures move no leg, so a trip under them has nothing to stop.
        resting = motion in _SOFT_RETURN_MOTIONS
        trips: list[OverloadTrip | None] = []
        if self._arm_overload is not None and arm:
            trips.append(self._arm_overload.sample(telemetry.current, arm))
        if self._leg_overload is not None:
            leg_trip = self._leg_overload.sample(telemetry.current, [m for m in LEG_MOTORS if m not in arm])
            leg_trip = self._resting_strain.filter(leg_trip, resting=resting, breached=self._leg_overload.strained)
            trips.append(leg_trip)
        rail_trip = None
        if self._rail is not None:
            rail_trip = self._rail.sample(telemetry.voltage, LEG_MOTORS)
            rail_trip = self._resting_sag.filter(rail_trip, resting=resting, breached=self._rail.sagging)
        found = [replace(t, motion=motion.name.lower()) for t in trips if t is not None]
        found_rail = replace(rail_trip, motion=motion.name.lower()) if rail_trip is not None else None
        for trip in found:
            self.overload_trips.record(trip)
            self._logger.warning(
                "overload guard (%s): %s at %d (threshold %d) for %d samples during %s; %s.",
                trip.channel,
                trip.motor,
                trip.current,
                trip.threshold,
                trip.samples,
                trip.motion,
                "no leg motion to stop" if resting else "stopping",
            )
        if found_rail is not None:
            self.rail_trips.record(found_rail)
            self._logger.warning(
                "rail guard: %.2f V at %s (threshold %.2f V) for %d samples during %s; %s.",
                found_rail.voltage,
                found_rail.motor,
                found_rail.threshold,
                found_rail.samples,
                found_rail.motion,
                "no leg motion to stop" if resting else "stopping",
            )
        if (not found and found_rail is None) or resting:
            return False
        if any(t.channel == "arm" for t in found):
            self._soft_return = arm
            self._soft_return_monitor.reset()
        return True

    def reset(self) -> None:
        """Forget all per-connection guard state (streaks, latch, poll timers, soft return).

        The trip histories survive: they are records, not connection state.
        """
        for guard in (self._arm_overload, self._leg_overload):
            if guard is not None:
                guard.reset()
        self._scope = None
        self._last_sample = None
        self._poller.reset()
        if self._rail is not None:
            self._rail.reset()
        self._resting_strain.reset()
        self._resting_sag.reset()
        self.end_soft_return()

    def end_soft_return(self) -> None:
        """Drop the soft-return state; the next gesture sync restores the arm's gain."""
        self._soft_return = None
        self._soft_return_monitor.reset()

    def abandon_soft_return(self) -> None:
        """Drop the soft-return axes without resetting the settle monitor."""
        self._soft_return = None
