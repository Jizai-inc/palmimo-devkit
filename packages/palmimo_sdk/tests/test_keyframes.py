"""Keyframe tracks — validation, interpolation, and playback through the Palmimo facade."""

import math
import re
from collections.abc import Sequence
from typing import cast

import pytest

from palmimo_sdk import MAX_JOINT_SPEED_DEG_S, Keyframe, Motion, MotionCancelled, Palmimo, RoutineStep, ServoDriver
from palmimo_sdk.engine import MotionEngine
from palmimo_sdk.io.base import SAFE_MIN_TICK, ServoTelemetry
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
