"""OverloadGuard and SoftReturnMonitor -- the pure overload classifiers."""

import pytest

from palmimo_sdk.kinematics import LEG_MOTORS, leg_motors
from palmimo_sdk.overload import (
    SOFT_RETURN_NEUTRAL_TICKS,
    SOFT_RETURN_SETTLE_SAMPLES,
    OverloadGuard,
    OverloadTrip,
    SoftReturnMonitor,
)


MOTORS = ("leg_1_yaw", "leg_1_pitch1", "leg_1_pitch2")


def _feed(guard: OverloadGuard, current: int, n: int, motor: str = "leg_1_yaw") -> OverloadTrip | None:
    verdict = None
    for _ in range(n):
        verdict = guard.sample({motor: current}, MOTORS)
    return verdict


def test_overload_guard_trips_on_the_third_consecutive_strained_sample() -> None:
    guard = OverloadGuard(700)
    assert _feed(guard, 700, 2) is None
    trip = guard.sample({"leg_1_yaw": 750}, MOTORS)
    assert trip is not None
    assert (trip.motor, trip.current, trip.threshold, trip.samples) == ("leg_1_yaw", 750, 700, 3)


def test_overload_guard_compares_magnitude_regardless_of_sign() -> None:
    assert _feed(OverloadGuard(700), -700, 3) is not None


def test_overload_guard_below_threshold_sample_resets_the_streak() -> None:
    guard = OverloadGuard(700)
    _feed(guard, 900, 2)
    guard.sample({"leg_1_yaw": 699}, MOTORS)
    assert _feed(guard, 900, 2) is None
    assert _feed(guard, 900, 1) is not None


def test_overload_guard_missing_axis_neither_advances_nor_resets() -> None:
    guard = OverloadGuard(700)
    _feed(guard, 900, 2)
    for _ in range(5):
        assert guard.sample({}, MOTORS) is None
    assert guard.sample({"leg_1_yaw": 900}, MOTORS) is not None


def test_overload_guard_ignores_axes_outside_the_watched_set() -> None:
    guard = OverloadGuard(700)
    for _ in range(5):
        assert guard.sample({"leg_2_yaw": 5000}, MOTORS) is None


def test_overload_guard_reports_the_first_watched_axis_when_several_trip_together() -> None:
    guard = OverloadGuard(700)
    currents = {"leg_1_pitch2": 900, "leg_1_yaw": 900}
    verdicts = [guard.sample(currents, MOTORS) for _ in range(3)]
    assert verdicts[2] is not None
    assert verdicts[2].motor == "leg_1_yaw"


def test_overload_guard_trip_clears_every_axis_streak() -> None:
    guard = OverloadGuard(700)
    currents = {"leg_1_yaw": 900, "leg_1_pitch1": 900}
    for _ in range(3):
        guard.sample(currents, MOTORS)
    # A second trip needs a whole new run, for the tripping axis and its neighbour alike.
    assert guard.sample(currents, MOTORS) is None
    assert guard.sample(currents, MOTORS) is None
    assert guard.sample(currents, MOTORS) is not None


def test_overload_guard_reset_forgets_streaks() -> None:
    guard = OverloadGuard(700)
    _feed(guard, 900, 2)
    guard.reset()
    assert _feed(guard, 900, 2) is None


def test_overload_guard_verdict_carries_the_channel_label() -> None:
    trip = _feed(OverloadGuard(700, channel="arm"), 900, 3)
    assert trip is not None
    assert trip.channel == "arm"
    assert trip.motion == ""  # the facade fills it in


@pytest.mark.parametrize("threshold", [0, -1])
def test_overload_guard_rejects_a_non_positive_threshold(threshold: int) -> None:
    with pytest.raises(ValueError, match="threshold"):
        OverloadGuard(threshold)


def test_overload_guard_rejects_a_zero_streak_length() -> None:
    with pytest.raises(ValueError, match="consecutive"):
        OverloadGuard(700, consecutive=0)


def test_leg_motors_expand_in_bus_order() -> None:
    assert leg_motors([2, 1]) == (
        "leg_2_yaw",
        "leg_2_pitch1",
        "leg_2_pitch2",
        "leg_1_yaw",
        "leg_1_pitch1",
        "leg_1_pitch2",
    )
    assert len(LEG_MOTORS) == 18


def _settled(motors: tuple[str, ...], tick: int) -> dict[str, int]:
    return dict.fromkeys(motors, tick)


def test_soft_return_monitor_releases_after_enough_settled_sweeps() -> None:
    monitor = SoftReturnMonitor(2048)
    home = _settled(MOTORS, 2048 + SOFT_RETURN_NEUTRAL_TICKS)
    results = [monitor.sample(home, MOTORS) for _ in range(SOFT_RETURN_SETTLE_SAMPLES)]
    assert results == [False] * (SOFT_RETURN_SETTLE_SAMPLES - 1) + [True]


def test_soft_return_monitor_holds_while_any_axis_is_outside_the_band() -> None:
    monitor = SoftReturnMonitor(2048)
    positions = _settled(MOTORS, 2048)
    positions["leg_1_pitch2"] = 2048 + SOFT_RETURN_NEUTRAL_TICKS + 1
    assert not any(monitor.sample(positions, MOTORS) for _ in range(10))


def test_soft_return_monitor_missing_axis_zeroes_the_streak() -> None:
    monitor = SoftReturnMonitor(2048)
    home = _settled(MOTORS, 2048)
    for _ in range(SOFT_RETURN_SETTLE_SAMPLES - 1):
        monitor.sample(home, MOTORS)
    assert monitor.sample({"leg_1_yaw": 2048}, MOTORS) is False
    assert not any(monitor.sample(home, MOTORS) for _ in range(SOFT_RETURN_SETTLE_SAMPLES - 1))
    assert monitor.sample(home, MOTORS) is True


def test_soft_return_monitor_empty_watch_list_never_releases() -> None:
    monitor = SoftReturnMonitor(2048)
    assert not any(monitor.sample({}, ()) for _ in range(10))


def test_soft_return_monitor_reset_forgets_the_streak() -> None:
    monitor = SoftReturnMonitor(2048, consecutive=2)
    home = _settled(MOTORS, 2048)
    monitor.sample(home, MOTORS)
    monitor.reset()
    assert monitor.sample(home, MOTORS) is False
