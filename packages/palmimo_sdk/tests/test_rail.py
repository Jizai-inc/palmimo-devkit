"""RailGuard -- the pure rail undervoltage classifier."""

import pytest

from palmimo_sdk.rail import RAIL_CONSECUTIVE, RailGuard, RailTrip


MOTORS = ("leg_1_yaw", "leg_1_pitch1", "leg_1_pitch2")
OK = dict.fromkeys(MOTORS, 4.2)


def _feed(guard: RailGuard, volts: float, n: int, motor: str = "leg_1_yaw") -> RailTrip | None:
    verdict = None
    for _ in range(n):
        verdict = guard.sample({**OK, motor: volts}, MOTORS)
    return verdict


def test_rail_guard_trips_after_consecutive_sagging_sweeps() -> None:
    guard = RailGuard(3.8)
    assert _feed(guard, 3.7, RAIL_CONSECUTIVE - 1) is None
    assert guard.sagging
    trip = guard.sample({**OK, "leg_1_yaw": 3.7}, MOTORS)
    assert trip is not None
    assert (trip.threshold, trip.samples, trip.motion) == (3.8, RAIL_CONSECUTIVE, "")


def test_rail_guard_sweep_at_the_threshold_resets_the_streak() -> None:
    guard = RailGuard(3.8)
    _feed(guard, 3.7, RAIL_CONSECUTIVE - 1)
    guard.sample(dict.fromkeys(MOTORS, 3.8), MOTORS)
    assert not guard.sagging
    assert _feed(guard, 3.7, RAIL_CONSECUTIVE - 1) is None


def test_rail_guard_reports_the_lowest_axis_and_its_voltage() -> None:
    guard = RailGuard(3.8, consecutive=1)
    trip = guard.sample({"leg_1_yaw": 3.7, "leg_1_pitch1": 3.4, "leg_1_pitch2": 3.6}, MOTORS)
    assert trip is not None
    assert (trip.motor, trip.voltage) == ("leg_1_pitch1", 3.4)


def test_rail_guard_sweep_missing_every_watched_axis_neither_advances_nor_resets() -> None:
    guard = RailGuard(3.8)
    _feed(guard, 3.7, RAIL_CONSECUTIVE - 1)
    for _ in range(5):
        assert guard.sample({"leg_2_yaw": 3.0}, MOTORS) is None
    assert guard.sample({**OK, "leg_1_yaw": 3.7}, MOTORS) is not None


def test_rail_guard_needs_a_fresh_streak_after_a_trip() -> None:
    guard = RailGuard(3.8)
    assert _feed(guard, 3.7, RAIL_CONSECUTIVE) is not None
    assert not guard.sagging
    assert _feed(guard, 3.7, RAIL_CONSECUTIVE - 1) is None
    assert _feed(guard, 3.7, 1) is not None


def test_rail_guard_rejects_invalid_arguments() -> None:
    with pytest.raises(ValueError, match="threshold"):
        RailGuard(0)
    with pytest.raises(ValueError, match="consecutive"):
        RailGuard(3.8, consecutive=0)
