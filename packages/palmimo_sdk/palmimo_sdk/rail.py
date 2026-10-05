# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Power-rail undervoltage guard — a pure classifier over ``ServoDriver.read_telemetry()``.

Resistance in the supply path pulls every axis's input voltage down together
when the summed current is high. A sag that persists makes the servos latch an
Input Voltage error and drop torque, which lets the body fall, while each axis's
own current stays inside its limit, so the per-axis overload guard cannot see it.
:class:`RailGuard` flags the sag a few samples early.

The guard only judges numbers it is handed. Reading the driver on a schedule and
acting on a verdict is :class:`~palmimo_sdk.robot.Palmimo`'s job (in
:meth:`~palmimo_sdk.robot.Palmimo.step`), which keeps this module free of I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


# Volts. The servo's Min Voltage Limit is 3.5 V; across a full-motion
# measurement of an assembled robot, the longest stretch below 3.8 V during
# normal walking was 67 ms.
RAIL_UNDERVOLTAGE_V = 3.8
# Samples in a row below the threshold before the guard trips (200 ms at the 30 Hz poll;
# up to about 300 ms when a poll slips to every third frame at 60 fps).
RAIL_CONSECUTIVE = 6


@dataclass(frozen=True)
class RailTrip:
    """One rail verdict: how far the supply sagged, and what the facade did about it.

    Attributes:
        voltage (float): Lowest input voltage in the tripping sweep, in volts.
        motor (str): Axis that read it.
        threshold (float): Voltage the rail was held below.
        samples (int): Consecutive sweeps below the threshold.
        motion (str): Motion that was running; filled in by the facade, ``""``
            straight out of :meth:`RailGuard.sample`.
    """

    voltage: float
    motor: str
    threshold: float
    samples: int
    motion: str


class RailGuard:
    """Trips when the lowest input voltage holds below a threshold.

    Every axis measures the same rail, so the sweep's lowest reading is one
    signal with one streak, not a streak per axis. A sweep at or above the
    threshold zeroes the streak.

    Args:
        threshold_v (float): Input voltage, in volts, below which the rail counts as sagging.
        consecutive (int): Sweeps in a row needed to trip.

    Raises:
        ValueError: *threshold_v* is not positive or *consecutive* is below 1.
    """

    def __init__(self, threshold_v: float, *, consecutive: int = RAIL_CONSECUTIVE) -> None:
        if threshold_v <= 0:
            raise ValueError("threshold_v must be positive (volts).")
        if consecutive < 1:
            raise ValueError("consecutive must be at least 1.")
        self._threshold = threshold_v
        self._consecutive = consecutive
        self._streak = 0

    @property
    def threshold(self) -> float:
        """Input voltage, in volts, below which the rail counts as sagging."""
        return self._threshold

    @property
    def sagging(self) -> bool:
        """Whether the last sweep read below the threshold (``False`` right after a trip)."""
        return self._streak > 0

    def sample(self, voltages: Mapping[str, float], motors: Sequence[str]) -> RailTrip | None:
        """Fold one sweep into the streak and return a verdict if the rail trips.

        Only *motors* are judged. A sweep with none of them in *voltages* read
        nothing, so the streak neither advances nor resets. A trip clears the
        streak, so the next trip needs a fresh run. When several axes read the
        same lowest voltage, the first in *motors* order is reported.

        Args:
            voltages (Mapping[str, float]): Axis name -> input voltage in volts.
            motors (Sequence[str]): Axes to judge, in tie-break order.
        """
        lowest: tuple[str, float] | None = None
        for motor in motors:
            if motor not in voltages:
                continue
            volts = float(voltages[motor])
            if lowest is None or volts < lowest[1]:
                lowest = (motor, volts)
        if lowest is None:
            return None
        motor, volts = lowest
        if volts >= self._threshold:
            self._streak = 0
            return None
        self._streak += 1
        if self._streak < self._consecutive:
            return None
        trip = RailTrip(volts, motor, self._threshold, self._streak, "")
        self._streak = 0
        return trip

    def reset(self) -> None:
        """Forget the streak."""
        self._streak = 0
