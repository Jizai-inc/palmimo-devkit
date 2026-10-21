"""Keyframe tracks — validation, interpolation, and playback through the Palmimo facade."""

import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from palmimo_sdk import (
    MAX_JOINT_SPEED_DEG_S,
    Keyframe,
    Motion,
    MotionCancelled,
    Palmimo,
    RoutineStep,
    ServoDriver,
    load_keyframes,
    save_keyframes,
)
from palmimo_sdk.engine import MotionEngine
from palmimo_sdk.io.base import SAFE_MIN_TICK, ServoPositions, ServoTelemetry
from palmimo_sdk.keyframes import JOINTS, KeyframeTrack, degrees_to_tick
from palmimo_sdk.kinematics import LEG_MOTORS, NEUTRAL
from palmimo_sdk.overload import OVERLOAD_POLL_INTERVAL_S
from palmimo_sdk.thermal import NECK_HOT_C, NECK_MOTORS


NEUTRAL_POSE = dict.fromkeys(JOINTS, NEUTRAL)


# ================================================================
# KeyframeTrack — pure validation and interpolation
# ================================================================


def test_track_reaches_each_keyframe_at_its_time() -> None:
    track = KeyframeTrack(NEUTRAL_POSE, [Keyframe(1.0, {"leg_6_pitch1": 30.0}), Keyframe(2.0, {"leg_6_pitch1": -10.0})])

    assert track.sample(0.0)["leg_6_pitch1"] == NEUTRAL
    assert track.sample(1.0)["leg_6_pitch1"] == degrees_to_tick(30.0)
    assert track.sample(2.0)["leg_6_pitch1"] == degrees_to_tick(-10.0)
    assert track.duration == 2.0


def test_track_eases_in_and_out_of_every_keyframe() -> None:
    """Raised-cosine: the joint barely moves in the first and last frame of a segment."""
    track = KeyframeTrack(NEUTRAL_POSE, [Keyframe(1.0, {"leg_6_pitch1": 30.0})])
    full = degrees_to_tick(30.0) - NEUTRAL
    dt = 1.0 / 60.0

    assert track.sample(dt)["leg_6_pitch1"] - NEUTRAL < 0.01 * full
    assert degrees_to_tick(30.0) - track.sample(1.0 - dt)["leg_6_pitch1"] < 0.01 * full
    assert track.sample(0.5)["leg_6_pitch1"] == NEUTRAL + round(full / 2)


def test_track_holds_a_joint_a_keyframe_leaves_out() -> None:
    track = KeyframeTrack(
        NEUTRAL_POSE,
        [
            Keyframe(1.0, {"neck_yaw": 10.0, "leg_6_pitch1": 20.0}),
            Keyframe(2.0, {"leg_6_pitch1": 0.0}),
            Keyframe(3.0, {"neck_yaw": 0.0}),
        ],
    )

    for t in (1.0, 1.5, 2.0):
        assert track.sample(t)["neck_yaw"] == degrees_to_tick(10.0)
    assert track.sample(3.0)["neck_yaw"] == NEUTRAL
    assert track.sample(2.5)["leg_6_pitch1"] == NEUTRAL  # still holding keyframe 1's 0 deg


def test_track_holds_the_end_pose_past_its_duration() -> None:
    track = KeyframeTrack(NEUTRAL_POSE, [Keyframe(0.5, {"leg_1_yaw": 15.0})])

    assert track.sample(10.0) == track.end_pose
    assert track.end_pose["leg_1_yaw"] == degrees_to_tick(15.0)


def test_track_starts_from_the_given_pose() -> None:
    start = {**NEUTRAL_POSE, "leg_2_pitch2": 2300}
    track = KeyframeTrack(start, [Keyframe(1.0, {"leg_1_yaw": 5.0})])

    assert track.sample(0.0) == start
    assert track.sample(1.0)["leg_2_pitch2"] == 2300


@pytest.mark.parametrize(
    ("keyframes", "message"),
    [
        ([], "at least one keyframe"),
        ([Keyframe(0.0, {"leg_1_yaw": 1.0})], "later than 0"),
        ([Keyframe(-1.0, {})], "later than 0"),
        ([Keyframe(1.0, {}), Keyframe(1.0, {})], "later than 1.0"),
        ([Keyframe(2.0, {}), Keyframe(1.0, {})], "later than 2.0"),
        ([Keyframe(math.inf, {})], "finite"),
        ([Keyframe(1.0, {"leg_7_yaw": 1.0})], "unknown joint 'leg_7_yaw'"),
        ([Keyframe(1.0, {"leg_1_yaw": math.nan})], "not a finite number"),
    ],
)
def test_track_rejects_malformed_keyframes(keyframes: list[Keyframe], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        KeyframeTrack(NEUTRAL_POSE, keyframes)


def test_track_rejects_a_leg_angle_outside_the_safe_tick_range() -> None:
    beyond = (SAFE_MIN_TICK - NEUTRAL - 1) * 360.0 / 4096.0
    with pytest.raises(ValueError, match=r"leg_3_pitch1 .* outside its range"):
        KeyframeTrack(NEUTRAL_POSE, [Keyframe(5.0, {"leg_3_pitch1": beyond})])


def test_track_keeps_the_neck_inside_the_engine_neck_band() -> None:
    """The legs' safe range would let the neck swing far past what its own motions are allowed."""
    band_deg = MotionEngine.NECK_AMPLITUDE_TICKS * 360.0 / 4096.0
    KeyframeTrack(NEUTRAL_POSE, [Keyframe(5.0, {"neck_yaw": band_deg - 0.5})])
    with pytest.raises(ValueError, match=r"neck_yaw .* outside its range"):
        KeyframeTrack(NEUTRAL_POSE, [Keyframe(5.0, {"neck_yaw": band_deg + 1.0})])


def test_track_keeps_neck_pitch1_within_the_amplitude_around_its_rest_trimmed_front() -> None:
    """pitch1's front sits below servo neutral, so the chin-down side ends sooner than the raw band."""
    amp = MotionEngine.NECK_AMPLITUDE_TICKS
    center = MotionEngine().neck_pitch_center()
    inside = (center + amp - NEUTRAL - 1) * 360.0 / 4096.0
    KeyframeTrack(NEUTRAL_POSE, [Keyframe(5.0, {"neck_pitch1": inside})], neck_pitch_center=center)
    with pytest.raises(ValueError, match=r"neck_pitch1 .* outside its range"):
        KeyframeTrack(NEUTRAL_POSE, [Keyframe(5.0, {"neck_pitch1": inside + 2.0})], neck_pitch_center=center)


def test_track_rejects_a_segment_faster_than_the_joint_speed_limit() -> None:
    fastest_s = (math.pi / 2) * 60.0 / MAX_JOINT_SPEED_DEG_S  # 60 deg at exactly the limit
    KeyframeTrack(NEUTRAL_POSE, [Keyframe(fastest_s + 0.01, {"leg_6_pitch1": 60.0})])
    with pytest.raises(ValueError, match=r"leg_6_pitch1 would move 60\.0 deg"):
        KeyframeTrack(NEUTRAL_POSE, [Keyframe(fastest_s - 0.01, {"leg_6_pitch1": 60.0})])


def test_track_checks_speed_from_the_start_pose_too() -> None:
    """A pose already far from the first keyframe makes the opening segment the fast one."""
    start = {**NEUTRAL_POSE, "leg_6_pitch1": degrees_to_tick(60.0)}
    with pytest.raises(ValueError, match="Keyframe 0: leg_6_pitch1"):
        KeyframeTrack(start, [Keyframe(0.2, {"leg_6_pitch1": 0.0})])


def test_track_checks_speed_of_a_held_joint_against_its_next_keyframe() -> None:
    """A joint left out of the middle keyframe moves only in the last segment, so that segment carries the speed."""
    keyframes = [Keyframe(1.0, {"leg_1_yaw": 0.0}), Keyframe(1.1, {"leg_6_pitch1": 0.0})]
    KeyframeTrack(NEUTRAL_POSE, keyframes)
    start = {**NEUTRAL_POSE, "leg_6_pitch1": degrees_to_tick(30.0)}
    with pytest.raises(ValueError, match="Keyframe 1: leg_6_pitch1"):
        KeyframeTrack(start, keyframes)


# ================================================================
# Palmimo.iter_keyframes / play_keyframes — compute-only
# ================================================================


def _neutral_robot(**kwargs: object) -> Palmimo:
    robot = Palmimo(**kwargs)  # type: ignore[arg-type]
    robot.return_to_neutral()
    return robot


def test_iter_keyframes_starts_from_the_current_pose() -> None:
    robot = _neutral_robot()
    before = robot.positions

    frames = list(robot.iter_keyframes([Keyframe(1.0, {"leg_6_pitch1": 30.0})]))

    assert frames[0] == before


def test_iter_keyframes_reaches_each_keyframe_on_its_frame() -> None:
    robot = _neutral_robot()

    frames = list(robot.iter_keyframes([Keyframe(1.0, {"leg_6_pitch1": 30.0}), Keyframe(1.5, {"neck_yaw": 5.0})]))

    assert frames[60]["leg_6_pitch1"] == degrees_to_tick(30.0)
    assert frames[90]["neck_yaw"] == degrees_to_tick(5.0)


def test_iter_keyframes_glides_home_and_idles_after_the_last_keyframe() -> None:
    robot = _neutral_robot()
    neutral = robot.positions

    frames = list(robot.iter_keyframes([Keyframe(1.0, {"leg_6_pitch1": 30.0, "neck_pitch1": -10.0})]))

    assert len(frames) == 60 * 2 + 1  # the track's second, then the one-second glide home
    assert frames[-1] == neutral
    assert robot.motion == "idle"
    assert robot.step() == neutral


def test_iter_keyframes_plays_at_the_facade_rate_whatever_engine_dt_was_left_at() -> None:
    """The speed check assumes the facade's fps; a stale engine.dt would play the track faster."""
    robot = _neutral_robot()
    robot.engine.dt = 1.0 / 30.0

    frames = list(robot.iter_keyframes([Keyframe(1.0, {"leg_6_pitch1": 30.0})]))

    assert frames[30]["leg_6_pitch1"] < degrees_to_tick(30.0)
    assert frames[60]["leg_6_pitch1"] == degrees_to_tick(30.0)


def test_closing_the_iterator_early_leaves_no_track_to_resume() -> None:
    robot = _neutral_robot()
    frames = robot.iter_keyframes([Keyframe(2.0, {"leg_6_pitch1": 30.0})])
    for _ in range(30):
        mid = next(frames)["leg_6_pitch1"]

    frames.close()  # type: ignore[attr-defined]

    assert robot.motion == "idle"
    after = [robot.step()["leg_6_pitch1"] for _ in range(60)]
    assert after[-1] == NEUTRAL
    assert all(abs(t - NEUTRAL) <= abs(mid - NEUTRAL) for t in after)


def test_an_error_from_the_callback_leaves_no_track_running() -> None:
    robot = _neutral_robot()
    seen: list[dict[str, int]] = []

    def fail_on_tenth(pos: dict[str, int]) -> None:
        seen.append(pos)
        if len(seen) == 10:
            raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        robot.play_keyframes([Keyframe(1.0, {"leg_6_pitch1": 30.0})], arm_legs=[6], callback=fail_on_tenth)

    assert robot.motion == "idle"
    assert robot._keyframe_arm_legs == ()


def test_keyframes_motion_cannot_be_selected_without_a_track() -> None:
    robot = _neutral_robot()
    list(robot.iter_keyframes([Keyframe(0.5, {"leg_6_pitch1": 10.0})]))  # a finished track stays loaded nowhere

    with pytest.raises(ValueError, match="load_keyframes"):
        robot.engine.motion = Motion.KEYFRAMES
    with pytest.raises(ValueError, match="play_keyframes"):
        list(robot.play([RoutineStep(motion=Motion.KEYFRAMES, seconds=1.0)]))


def test_iter_keyframes_validates_before_the_first_frame() -> None:
    """A bad track raises on the call itself, not when the first frame is pulled."""
    robot = _neutral_robot()
    with pytest.raises(ValueError, match="unknown joint"):
        robot.iter_keyframes([Keyframe(1.0, {"tail": 1.0})])
    with pytest.raises(ValueError, match="arm_legs must be leg IDs 1-6"):
        robot.iter_keyframes([Keyframe(1.0, {})], arm_legs=[0, 6])
    assert robot.motion == "idle"


def test_iter_keyframes_ends_when_another_motion_takes_over() -> None:
    robot = _neutral_robot()
    frames = robot.iter_keyframes([Keyframe(2.0, {"leg_6_pitch1": 30.0})])
    for _ in range(10):
        next(frames)

    robot.forward()

    assert list(frames) == []
    assert robot.motion == "forward"


def test_play_keyframes_cancel_glides_the_legs_home_and_raises() -> None:
    robot = _neutral_robot()
    seen: list[dict[str, int]] = []

    def cancel_after_ten(pos: dict[str, int]) -> None:
        seen.append(pos)
        if len(seen) == 10:
            robot.cancel()

    with pytest.raises(MotionCancelled):
        robot.play_keyframes([Keyframe(1.0, {"leg_6_pitch1": 30.0})], callback=cancel_after_ten)

    assert len(seen) == 10
    assert robot.motion == "idle"
    assert abs(robot.positions["leg_6_pitch1"] - NEUTRAL) < abs(seen[-1]["leg_6_pitch1"] - NEUTRAL)


def test_play_keyframes_returns_the_final_pose() -> None:
    robot = _neutral_robot()
    neutral = robot.positions

    assert robot.play_keyframes([Keyframe(0.1, {"leg_1_yaw": 2.0})]) == neutral


# ================================================================
# Guards stay active during playback
# ================================================================


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class _TelemetryDriver(ServoDriver):
    """An in-memory driver reporting injected leg currents and neck temperatures."""

    def __init__(self) -> None:
        self.currents: dict[str, int] = {}
        self.temperatures: dict[str, int] = dict.fromkeys(NECK_MOTORS, 40)
        self.writes: list[dict[str, int]] = []
        self.tuning: list[tuple[str, object, object]] = []
        self.profile_velocity = 300
        self._connected = True

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def _write_positions(self, positions: dict[str, int]) -> None:
        self.writes.append(dict(positions))

    def read_telemetry(self, motors: Sequence[str] | None = None) -> ServoTelemetry:
        wanted = list(motors) if motors is not None else [*LEG_MOTORS, *NECK_MOTORS]
        return ServoTelemetry(
            current={m: self.currents[m] for m in wanted if m in self.currents},
            voltage={m: 5.0 for m in wanted if m in LEG_MOTORS},
            temperature={m: self.temperatures[m] for m in wanted if m in self.temperatures},
        )

    # RAM tuning, recorded so a test can tell which gesture tuning was applied.
    def set_profile_velocity_units(self, value: int, motors: list[str] | None = None) -> None:
        self.tuning.append(("pv", value, motors))

    def set_position_p_gain(self, value: int | None, motors: list[str] | None = None) -> None:
        self.tuning.append(("gain", value, motors))

    def set_iir(self, enabled: bool, alpha: float | None = None, motors: list[str] | None = None) -> None:
        self.tuning.append(("iir", enabled, motors))


def _guarded_robot(driver: _TelemetryDriver) -> tuple[Palmimo, _FakeClock]:
    clock = _FakeClock()
    robot = Palmimo(driver=cast(ServoDriver, driver), thermal_clock=clock, auto_wake=False)
    robot.return_to_neutral()
    return robot, clock


def _pull(frames: object, clock: _FakeClock, n: int) -> list[dict[str, int]]:
    """Pull up to *n* frames, each a poll interval apart on the injected clock."""
    out: list[dict[str, int]] = []
    it = iter(cast("list[dict[str, int]]", frames))
    for _ in range(n):
        try:
            out.append(next(it))
        except StopIteration:
            break
        clock.value += 2 * OVERLOAD_POLL_INTERVAL_S
    return out


def test_arm_overload_trip_ends_playback_for_a_declared_arm_leg() -> None:
    driver = _TelemetryDriver()
    robot, clock = _guarded_robot(driver)
    frames = robot.iter_keyframes([Keyframe(2.0, {"leg_6_pitch1": 30.0})], arm_legs=[6])
    driver.currents = {"leg_6_pitch1": 800}  # above the arm threshold (700), below the leg one (1200)

    pulled = _pull(frames, clock, 200)

    assert 30 < len(pulled) <= 30 + 5  # trips within a few strained samples, then the 0.5 s settle
    assert robot.motion == "idle"
    (trip,) = robot.drain_overload_trips()
    assert (trip.motor, trip.channel, trip.motion) == ("leg_6_pitch1", "arm", "keyframes")


def test_guard_trip_mid_track_glides_the_legs_toward_neutral() -> None:
    """The trip must not leave the leg frozen in the pose that strained it."""
    driver = _TelemetryDriver()
    robot, clock = _guarded_robot(driver)
    frames = iter(robot.iter_keyframes([Keyframe(2.0, {"leg_6_pitch1": 60.0})]))
    raised = _pull(frames, clock, 60)[-1]["leg_6_pitch1"]
    driver.currents = {"leg_6_pitch1": 1300}  # above the weight-bearing threshold

    after = _pull(frames, clock, 200)

    assert robot.motion == "idle"
    assert (robot.drain_overload_trips()[0].channel, robot.motion) == ("leg", "idle")
    final = driver.writes[-1]["leg_6_pitch1"]
    assert abs(final - NEUTRAL) < 0.1 * abs(raised - NEUTRAL)
    assert 30 < len(after) <= 30 + 5


def test_guard_trip_on_the_last_frame_still_settles_the_legs() -> None:
    driver = _TelemetryDriver()
    robot, clock = _guarded_robot(driver)
    frames = iter(robot.iter_keyframes([Keyframe(1.0, {"leg_6_pitch1": 60.0})]))
    _pull(frames, clock, 118)  # the trip lands on the 3rd strained frame: the 121st and last (glide home included)
    driver.currents = {"leg_6_pitch1": 1300}

    after = _pull(frames, clock, 200)

    assert robot.drain_overload_trips()[0].motion == "keyframes"
    assert len(after) == 3 + 30  # the settle ran after the track's last frame
    assert abs(driver.writes[-1]["leg_6_pitch1"] - NEUTRAL) < 0.1 * (degrees_to_tick(60.0) - NEUTRAL)


def test_arm_legs_do_not_apply_the_wave_servo_tuning() -> None:
    """Only the overload threshold changes; the wave's IIR would make the arm lag the validated curve."""
    driver = _TelemetryDriver()
    robot, clock = _guarded_robot(driver)

    _pull(robot.iter_keyframes([Keyframe(0.5, {"leg_6_pitch1": 20.0})], arm_legs=[6]), clock, 200)

    assert driver.tuning == []


def test_undeclared_raised_leg_is_judged_by_the_weight_bearing_threshold() -> None:
    driver = _TelemetryDriver()
    robot, clock = _guarded_robot(driver)
    frames = robot.iter_keyframes([Keyframe(0.5, {"leg_6_pitch1": 10.0})])
    driver.currents = {"leg_6_pitch1": 800}

    pulled = _pull(frames, clock, 200)

    assert len(pulled) == 60 * 3 // 2 + 1  # played through, glide home included
    assert robot.drain_overload_trips() == []


def test_thermal_lock_takes_the_neck_out_of_the_track() -> None:
    driver = _TelemetryDriver()
    driver.temperatures = dict.fromkeys(NECK_MOTORS, NECK_HOT_C)
    robot, clock = _guarded_robot(driver)
    center = robot.engine.neck_pitch_center()

    pulled = _pull(robot.iter_keyframes([Keyframe(1.0, {"neck_yaw": 20.0, "leg_6_pitch1": 20.0})]), clock, 61)

    assert robot.neck_lock_active
    assert pulled[-1]["neck_yaw"] == NEUTRAL
    assert pulled[-1]["neck_pitch1"] == center
    assert pulled[-1]["leg_6_pitch1"] == degrees_to_tick(20.0)  # the legs still play


# ================================================================
# Recording by hand: read_pose / limp / save_keyframes / load_keyframes
# ================================================================


class _PoseDriver(ServoDriver):
    """An in-memory driver with settable present positions that logs writes, gain and profile changes in order."""

    def __init__(self) -> None:
        self.present: dict[str, int] = dict.fromkeys(JOINTS, NEUTRAL)
        self.silent: set[str] = set()
        self.log: list[tuple[str, object]] = []
        self.profile_velocity = 300
        self._connected = True

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def _write_positions(self, positions: dict[str, int]) -> None:
        self.log.append(("write", dict(positions)))

    def read_positions_span(self, motors: Sequence[str] | None = None) -> ServoPositions:
        """Sweep in servo-ID order and stop at the first silent servo, as the real bus does."""
        wanted = sorted(motors if motors is not None else JOINTS, key=JOINTS.index)
        positions: dict[str, int] = {}
        for i, motor in enumerate(wanted):
            if motor in self.silent:
                return ServoPositions(positions=positions, silent=(motor,), unreached=tuple(wanted[i + 1 :]))
            positions[motor] = self.present[motor]
        return ServoPositions(positions=positions)

    def set_position_p_gain(self, value: int | None, motors: list[str] | None = None) -> None:
        self.log.append(("gain", (value, tuple(motors or ()))))

    def set_iir(self, enabled: bool, alpha: float | None = None, motors: list[str] | None = None) -> None:
        self.log.append(("iir", enabled))

    def set_profile_velocity_units(self, value: int, motors: list[str] | None = None) -> None:
        self.log.append(("pv", (value, tuple(motors or ()))))


def _pose_robot() -> tuple[Palmimo, _PoseDriver]:
    driver = _PoseDriver()
    robot = Palmimo(driver=cast(ServoDriver, driver), auto_wake=False)
    return robot, driver


def test_read_pose_reports_degrees_from_neutral() -> None:
    robot, driver = _pose_robot()
    driver.present["leg_6_pitch1"] = degrees_to_tick(-30.0)

    pose = robot.read_pose(["leg_6_pitch1", "leg_6_yaw"])

    assert list(pose) == ["leg_6_pitch1", "leg_6_yaw"]
    assert pose["leg_6_pitch1"] == pytest.approx(-30.0, abs=0.05)
    assert pose["leg_6_yaw"] == 0.0


def test_read_pose_raises_instead_of_reporting_a_silent_joint_as_neutral() -> None:
    robot, driver = _pose_robot()
    driver.silent = {"leg_6_pitch2"}

    with pytest.raises(RuntimeError, match="leg_6_pitch2"):
        robot.read_pose(["leg_6_pitch1", "leg_6_pitch2"])


def test_read_pose_needs_a_connected_driver_and_known_joints() -> None:
    with pytest.raises(RuntimeError, match="connected driver"):
        Palmimo().read_pose()
    robot, _ = _pose_robot()
    with pytest.raises(ValueError, match="leg_7_yaw"):
        robot.read_pose(["leg_7_yaw"])


def test_limp_holds_in_place_before_dropping_and_before_restoring_the_gain() -> None:
    robot, driver = _pose_robot()
    leg = ["leg_6_yaw", "leg_6_pitch1", "leg_6_pitch2"]

    with robot.limp(leg):
        driver.present["leg_6_pitch1"] = degrees_to_tick(-60.0)  # moved by hand
        driver.log.append(("hand", None))

    kinds = [kind for kind, _ in driver.log]
    assert kinds == ["write", "gain", "hand", "pv", "write", "gain", "pv"]
    assert driver.log[1][1] == (0, tuple(leg))
    held = cast(dict[str, int], driver.log[4][1])
    assert held["leg_6_pitch1"] == degrees_to_tick(-60.0)
    assert driver.log[5][1] == (None, tuple(leg))
    assert robot.positions["leg_6_pitch1"] == degrees_to_tick(-60.0)  # engine seeded


def test_limp_exit_holds_with_the_profile_off_so_the_gain_returns_onto_the_present_position() -> None:
    """A time-based profile would start the hold from where the joint went limp and sweep it back."""
    robot, driver = _pose_robot()
    leg = ["leg_6_pitch1", "leg_6_pitch2"]

    with robot.limp(leg):
        driver.log.clear()

    assert driver.log[0] == ("pv", (0, tuple(leg)))
    assert [kind for kind, _ in driver.log[1:3]] == ["write", "gain"]
    assert driver.log[-1] == ("pv", (300, tuple(leg)))


def test_limp_then_play_keyframes_starts_from_the_hand_posed_position() -> None:
    robot, driver = _pose_robot()
    with robot.limp(["leg_6_pitch1"]):
        driver.present["leg_6_pitch1"] = degrees_to_tick(-40.0)

    frames = list(robot.iter_keyframes([Keyframe(1.0, {"leg_6_pitch1": 0.0})]))

    assert frames[0]["leg_6_pitch1"] == degrees_to_tick(-40.0)


def test_limp_leaves_the_gain_at_zero_when_it_cannot_read_where_the_joint_is() -> None:
    """Restoring stiffness toward a stale goal would snap the joint; staying limp does not."""
    robot, driver = _pose_robot()

    with pytest.raises(RuntimeError, match="leg_6_yaw"), robot.limp(["leg_6_yaw"]):
        driver.silent = {"leg_6_yaw"}

    gains = [value for kind, value in driver.log if kind == "gain"]
    assert gains == [(0, ("leg_6_yaw",))]


def test_limp_rejects_no_joints_and_unknown_joints() -> None:
    robot, _ = _pose_robot()
    with pytest.raises(ValueError, match="at least one"), robot.limp([]):
        pass
    with pytest.raises(ValueError, match="tail"), robot.limp(["tail"]):
        pass


def test_saved_keyframes_load_back_equal(tmp_path: Path) -> None:
    track = [Keyframe(1.5, {"leg_6_pitch1": -61.264, "leg_6_yaw": 5.0}), Keyframe(3.0, {"neck_yaw": 10.0})]
    path = tmp_path / "track.json"

    save_keyframes(path, track)

    assert load_keyframes(path) == [
        Keyframe(1.5, {"leg_6_pitch1": -61.26, "leg_6_yaw": 5.0}),
        Keyframe(3.0, {"neck_yaw": 10.0}),
    ]


@pytest.mark.parametrize(
    "content",
    ['{"time": 1}', '[{"time": "1", "joints": {}}]', '[{"time": 1, "joints": {"leg_1_yaw": "x"}}]', "[3]"],
)
def test_load_keyframes_rejects_a_malformed_file(tmp_path: Path, content: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(content)
    with pytest.raises(ValueError, match=r"bad.json"):
        load_keyframes(path)


def test_limp_exit_restores_the_joints_that_answered_and_leaves_only_the_silent_one_limp() -> None:
    """One silent leg servo must not leave the neck at zero gain against its entry goal."""
    robot, driver = _pose_robot()
    joints = ["leg_6_yaw", "neck_yaw"]

    with pytest.raises(RuntimeError, match="leg_6_yaw"), robot.limp(joints):
        driver.present["neck_yaw"] = degrees_to_tick(20.0)
        driver.silent = {"leg_6_yaw"}

    writes = [cast(dict[str, int], v) for kind, v in driver.log if kind == "write"]
    gains = [v for kind, v in driver.log if kind == "gain"]
    assert writes[-1]["neck_yaw"] == degrees_to_tick(20.0)
    assert gains == [(0, tuple(joints)), (None, ("neck_yaw",))]


def test_limp_writes_nothing_when_a_joint_outside_it_does_not_answer() -> None:
    """The hold covers every joint, so a silent one would get the engine's stored pose as its goal."""
    robot, driver = _pose_robot()
    driver.silent = {"leg_1_yaw"}

    with pytest.raises(RuntimeError, match="leg_1_yaw"), robot.limp(["leg_6_yaw"]):
        pass

    assert [kind for kind, _ in driver.log if kind in ("write", "gain")] == []


def test_limp_exit_stays_limp_when_a_joint_outside_it_stops_answering() -> None:
    robot, driver = _pose_robot()

    with pytest.raises(RuntimeError, match="leg_1_yaw"), robot.limp(["leg_6_yaw"]):
        driver.silent = {"leg_1_yaw"}
        driver.log.clear()

    assert [kind for kind, _ in driver.log if kind in ("write", "gain")] == []


def test_limp_after_a_wave_releases_the_arm_filter_before_holding() -> None:
    """Holding through a still-enabled arm IIR would command a stale, filtered goal."""
    robot, driver = _pose_robot()
    robot.wave()
    for _ in range(5):
        robot.step()
    driver.log.clear()

    with robot.limp(["leg_6_pitch1"]):
        pass

    kinds = [kind for kind, _ in driver.log]
    assert kinds.index("iir") < kinds.index("write")
    assert ("iir", False) in driver.log


def test_saved_angles_load_back_to_the_same_ticks(tmp_path: Path) -> None:
    from palmimo_sdk.keyframes import tick_to_degrees

    ticks = range(SAFE_MIN_TICK, 3901)
    path = tmp_path / "all.json"
    save_keyframes(path, [Keyframe(1.0, {f"t{t}": tick_to_degrees(t) for t in ticks})])

    loaded = load_keyframes(path)[0].joints
    assert all(degrees_to_tick(loaded[f"t{t}"]) == t for t in ticks)


def test_load_keyframes_rejects_a_boolean_angle(tmp_path: Path) -> None:
    path = tmp_path / "bool.json"
    path.write_text('[{"time": 1, "joints": {"leg_6_yaw": true}}]')
    with pytest.raises(ValueError, match=r"bool\.json"):
        load_keyframes(path)


def test_limp_exit_reads_the_neck_behind_a_silent_leg_servo() -> None:
    """A sweep stops at the first silent servo; the neck (last in ID order) must still be read and held."""
    robot, driver = _pose_robot()
    joints = ["leg_6_yaw", "leg_6_pitch1", "neck_yaw"]

    with pytest.raises(RuntimeError, match=r"\['leg_6_yaw'\]"), robot.limp(joints):
        driver.present["neck_yaw"] = degrees_to_tick(30.0)
        driver.silent = {"leg_6_yaw"}

    writes = [cast(dict[str, int], v) for kind, v in driver.log if kind == "write"]
    gains = [v for kind, v in driver.log if kind == "gain"]
    assert writes[-1]["neck_yaw"] == degrees_to_tick(30.0)
    assert gains[-1] == (None, ("leg_6_pitch1", "neck_yaw"))


def test_neck_park_leaves_a_neck_axis_limp_left_alone() -> None:
    """Raising a limp-left axis's gain would snap it to its stale goal."""
    robot, driver = _pose_robot()
    with pytest.raises(RuntimeError), robot.limp(["neck_yaw"]):
        driver.silent = {"neck_yaw"}
    driver.log.clear()

    robot._park_neck()

    parked = {cast("tuple[int | None, tuple[str, ...]]", v)[1] for kind, v in driver.log if kind == "gain"}
    assert parked == {("neck_pitch1", "neck_pitch2")}


def test_limp_restores_the_gain_when_dropping_it_fails_part_way() -> None:
    robot, driver = _pose_robot()
    real = driver.set_position_p_gain

    def fail_on_zero(value: int | None, motors: list[str] | None = None) -> None:
        real(value, motors)
        if value == 0:
            raise OSError("bus error part-way through the gain write")

    driver.set_position_p_gain = fail_on_zero  # type: ignore[method-assign]

    with pytest.raises(OSError), robot.limp(["leg_6_yaw", "leg_6_pitch1"]):
        pytest.fail("the body must not run when the release failed")

    gains = [v for kind, v in driver.log if kind == "gain"]
    assert gains[-1] == (None, ("leg_6_yaw", "leg_6_pitch1"))


def _parked_axes(driver: _PoseDriver) -> set[tuple[str, ...]]:
    return {cast("tuple[int | None, tuple[str, ...]]", v)[1] for kind, v in driver.log if kind == "gain"}


def test_a_later_successful_limp_does_not_forget_a_neck_axis_still_left_limp() -> None:
    robot, driver = _pose_robot()
    with pytest.raises(RuntimeError), robot.limp(["neck_yaw"]):
        driver.silent = {"neck_yaw"}
    with robot.limp(["leg_6_yaw"]):
        pass
    driver.log.clear()

    robot._park_neck()

    assert _parked_axes(driver) == {("neck_pitch1", "neck_pitch2")}


def test_neck_park_holds_and_parks_a_limp_left_axis_that_answers_again() -> None:
    """Once the axis answers (and something may have stiffened it), hold it where it is and park it."""
    robot, driver = _pose_robot()
    with pytest.raises(RuntimeError), robot.limp(["neck_yaw"]):
        driver.silent = {"neck_yaw"}
    driver.silent = set()
    driver.present["neck_yaw"] = degrees_to_tick(25.0)
    driver.log.clear()

    robot._park_neck()

    writes = [cast(dict[str, int], v) for kind, v in driver.log if kind == "write"]
    assert writes[0]["neck_yaw"] == degrees_to_tick(25.0)
    assert _parked_axes(driver) == {("neck_pitch1", "neck_pitch2", "neck_yaw")}
    assert [kind for kind, _ in driver.log].index("write") < [kind for kind, _ in driver.log].index("gain")


def test_a_read_error_on_limp_exit_still_marks_the_joints_left_limp() -> None:
    robot, driver = _pose_robot()

    def broken(motors: Sequence[str] | None = None) -> ServoPositions:
        raise OSError("serial error")

    with pytest.raises(OSError), robot.limp(["neck_yaw"]):
        driver.read_positions_span = broken  # type: ignore[method-assign]
    driver.log.clear()

    robot._park_neck()

    assert _parked_axes(driver) == {("neck_pitch1", "neck_pitch2")}


def test_holding_a_joint_commands_every_other_joint_to_where_it_is_not_to_a_stale_engine_pose() -> None:
    """The engine's stored pose can lag a glide cut short; writing it would move the other legs."""
    robot, driver = _pose_robot()
    robot.engine.set_leg_positions({"leg_3_pitch1": degrees_to_tick(-40.0)})  # stale engine pose
    driver.present["leg_3_pitch1"] = degrees_to_tick(-10.0)  # where the leg really is

    with robot.limp(["leg_6_yaw"]):
        pass

    writes = [cast(dict[str, int], v) for kind, v in driver.log if kind == "write"]
    assert all(w["leg_3_pitch1"] == degrees_to_tick(-10.0) for w in writes)
    assert robot.positions["leg_3_pitch1"] == degrees_to_tick(-10.0)


def test_a_failed_profile_off_on_limp_exit_raises_and_leaves_the_joints_limp() -> None:
    """Holding with the profile still on sweeps the joint back, so the gain must not return."""
    robot, driver = _pose_robot()
    leg = ["leg_6_yaw", "leg_6_pitch1"]
    real = driver.set_profile_velocity_units

    def fail_on_zero(value: int, motors: list[str] | None = None) -> None:
        if value == 0:
            driver.log.append(("pv", (0, ("leg_6_yaw",))))
            raise OSError("bus error after the first motor")
        real(value, motors)

    driver.set_profile_velocity_units = fail_on_zero  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="profile off"), robot.limp(leg):
        driver.log.clear()

    assert driver.log[-1] == ("pv", (300, tuple(leg)))  # the profile still comes back
    assert ("gain", (None, tuple(leg))) not in driver.log
    assert robot._limp_left == frozenset(leg)


def test_gain_writes_skip_a_joint_a_failed_limp_exit_left_behind() -> None:
    """wake(), set_p_gain() and the wave tuning must not stiffen it toward its stale goal."""
    robot, driver = _pose_robot()
    with pytest.raises(RuntimeError), robot.limp(["neck_yaw"]):
        driver.silent = {"neck_yaw"}
    driver.silent = set()  # answering again: wake() refuses to move from an unread joint
    driver.log.clear()

    robot.set_p_gain(1200)
    robot.wake(duration=0.05)
    robot.wave()
    robot.step()

    gains = [cast("tuple[int | None, tuple[str, ...]]", v) for kind, v in driver.log if kind == "gain"]
    stiffened = [motors for value, motors in gains if value != 0]
    assert stiffened
    # An empty subset is the driver's "every motor" write, which includes the neck.
    assert all(motors and "neck_yaw" not in motors for motors in stiffened)


def test_speed_divides_every_keyframe_time_but_not_the_glide_home() -> None:
    robot = _neutral_robot()

    frames = list(robot.iter_keyframes([Keyframe(1.5, {"leg_6_pitch1": 30.0})], speed=1.5))

    assert len(frames) == 60 * 2 + 1  # 1.5 s at 1.5x = 1 s, then the 1 s glide home
    assert frames[60]["leg_6_pitch1"] == degrees_to_tick(30.0)


def test_speed_that_breaks_the_joint_speed_limit_raises_before_moving() -> None:
    robot = _neutral_robot()
    track = [Keyframe(0.5, {"leg_6_pitch1": 60.0})]  # fine at 1x (188 deg/s)

    with pytest.raises(ValueError, match="leg_6_pitch1 would move"):
        robot.iter_keyframes(track, speed=3.0)


def test_a_speed_too_fast_reports_the_fastest_speed_the_track_allows() -> None:
    robot = _neutral_robot()
    track = [Keyframe(1.0, {"leg_6_pitch1": 60.0})]  # peaks at about 94 deg/s at 1x

    with pytest.raises(ValueError, match=r"Too fast at x5: this track plays at up to x(\d+\.\d+)") as caught:
        robot.iter_keyframes(track, speed=5.0)

    found = re.search(r"up to x(\d+\.\d+)", str(caught.value))
    assert found is not None
    fastest = float(found.group(1))
    robot.iter_keyframes(track, speed=fastest)  # the reported speed plays
    with pytest.raises(ValueError):
        robot.iter_keyframes(track, speed=fastest + 0.02)


def test_an_out_of_range_track_keeps_its_own_message_at_any_speed() -> None:
    robot = _neutral_robot()
    with pytest.raises(ValueError, match="outside its range"):
        robot.iter_keyframes([Keyframe(1.0, {"neck_yaw": 60.0})], speed=2.0)


@pytest.mark.parametrize("speed", [0.0, -1.0, math.inf, math.nan])
def test_speed_must_be_a_positive_finite_number(speed: float) -> None:
    with pytest.raises(ValueError, match="speed"):
        _neutral_robot().iter_keyframes([Keyframe(1.0, {})], speed=speed)


def test_a_routine_with_a_keyframes_cue_fails_before_any_cue_moves() -> None:
    robot = _neutral_robot()
    routine = [RoutineStep(motion=Motion.FORWARD, seconds=1.0), RoutineStep(motion=Motion.KEYFRAMES, seconds=1.0)]

    with pytest.raises(ValueError, match="play_keyframes"):
        next(robot.play(routine))

    assert robot.motion == "idle"


def test_an_interrupt_inside_limp_is_not_replaced_by_a_failed_exit() -> None:
    """Ctrl-C while posing must reach the caller even if the exit cannot read a joint."""
    robot, driver = _pose_robot()

    with pytest.raises(KeyboardInterrupt), robot.limp(["neck_yaw"]):
        driver.silent = {"neck_yaw"}
        raise KeyboardInterrupt

    assert "neck_yaw" in robot._limp_left


def test_a_joint_posed_outside_the_safe_range_stays_limp_on_exit() -> None:
    """The driver would clamp its hold goal, and the returning gain would drive it there."""
    robot, driver = _pose_robot()

    with pytest.raises(RuntimeError, match="safe tick range"), robot.limp(["leg_6_pitch1", "leg_6_yaw"]):
        driver.present["leg_6_pitch1"] = SAFE_MIN_TICK - 50

    gains = [cast("tuple[int | None, tuple[str, ...]]", v) for kind, v in driver.log if kind == "gain"]
    assert gains[-1] == (None, ("leg_6_yaw",))
    assert robot._limp_left == frozenset({"leg_6_pitch1"})


def test_neck_park_holds_a_recovered_limp_axis_with_the_profile_off() -> None:
    robot, driver = _pose_robot()
    with pytest.raises(RuntimeError), robot.limp(["neck_yaw"]):
        driver.silent = {"neck_yaw"}
    driver.silent = set()
    driver.log.clear()

    robot._park_neck()

    kinds = [(kind, v) for kind, v in driver.log if kind in ("pv", "write")]
    assert kinds[0] == ("pv", (0, ("neck_yaw",)))
    assert kinds[1][0] == "write"


def test_limp_exit_holds_through_a_user_enabled_iir_without_blending_the_old_pose() -> None:
    from palmimo_sdk.io.dynamixel import DynamixelDriver

    driver = DynamixelDriver.__new__(DynamixelDriver)
    driver._iir_enabled, driver._iir_alpha, driver._iir_motors = True, 0.2, None
    driver._iir_state = {name: float(NEUTRAL) for name in JOINTS}  # pre-limp filter state
    filtered = driver._apply_iir

    robot, fake = _pose_robot()
    fake.iir_enabled = True  # type: ignore[attr-defined]
    fake.iir_alpha = 0.2  # type: ignore[attr-defined]

    def set_iir(enabled: bool, alpha: float | None = None, motors: list[str] | None = None) -> None:
        driver.set_iir(enabled, alpha, motors)
        fake.iir_enabled = enabled  # type: ignore[attr-defined]

    fake.set_iir = set_iir  # type: ignore[method-assign]
    real_write = fake._write_positions
    fake._write_positions = lambda pos: real_write(filtered(pos) if driver._iir_enabled else pos)  # type: ignore[method-assign, assignment]

    with robot.limp(["leg_6_pitch1"]):
        fake.present["leg_6_pitch1"] = degrees_to_tick(-40.0)

    hold = cast(dict[str, int], [v for kind, v in fake.log if kind == "write"][-1])
    assert hold["leg_6_pitch1"] == degrees_to_tick(-40.0)
    assert driver._iir_enabled  # the user's filter is back on
