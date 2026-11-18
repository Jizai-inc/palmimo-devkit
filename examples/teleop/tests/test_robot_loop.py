"""Behavior of `palmimo_teleop.robot_loop.RobotLoop`, driven deterministically via `tick()`
and `shutdown()` -- never through the real background thread or real time."""

import itertools
import threading
import time
from typing import Any, cast

import pytest

from palmimo_sdk import Palmimo
from palmimo_teleop.control import LEG_MOTORS, MAX_GAIT_SPEED
from palmimo_teleop.robot_loop import (
    FAILURE_LOG_INTERVAL_S,
    LEG_PROFILE_RETRY_BASE_S,
    LEG_PROFILE_RETRY_MAX_S,
    LEG_PROFILE_SETTLE_ATTEMPTS,
    NECK_PITCH_SIGN,
    NECK_YAW_SIGN,
    RobotLoop,
    SettleFailedError,
)
from palmimo_teleop.session import PilotSession


class FakeEngine:
    def __init__(self, gait_speed: float) -> None:
        self.gait_speed = gait_speed


class PvOnlyDriver:
    """A time-based-profile servo driver with no gain/IIR knobs; profile writes land in the robot's
    call log, and `pv` holds what each leg servo would really hold.

    Like `DynamixelDriver`, a `motors=` write goes motor by motor, so `fail_after` can make one
    fail partway: the first *n* motors take the value, then it raises.
    """

    profile_velocity = 300

    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self._calls = calls
        self.pv: dict[str, int] = dict.fromkeys(LEG_MOTORS, 300)
        self.write_attempts = 0

    fail_next_writes = 0
    fail_after: int | None = None
    always_fail = False

    def set_profile_velocity_units(self, value: int, motors: list[str] | None = None) -> None:
        self.write_attempts += 1
        targets = list(motors) if motors else list(LEG_MOTORS)
        if self.always_fail:
            raise ConnectionError("no status packet")
        if self.fail_next_writes:
            self.fail_next_writes -= 1
            raise ConnectionError("no status packet")
        if self.fail_after is not None:
            reached, self.fail_after = self.fail_after, None
            for motor in targets[:reached]:
                self.pv[motor] = value
            raise ConnectionError("no status packet")
        for motor in targets:
            self.pv[motor] = value
        self._calls.append(("profile_velocity", (value, tuple(motors) if motors else None)))


class FakeDriver(PvOnlyDriver):
    """A driver with the Position_P_Gain / IIR knobs, so `Palmimo` tunes a wave on it itself."""

    def set_position_p_gain(self, value: int | None, motors: list[str] | None = None) -> None:
        return None

    def set_iir(self, enabled: bool, alpha: float | None = None, motors: list[str] | None = None) -> None:
        return None


class FakeRobot:
    """Records every call `RobotLoop` makes, in order, and holds the speed knobs it writes."""

    def __init__(
        self, *, gait_speed: float = 0.012, with_driver: bool = True, driver_cls: type[PvOnlyDriver] | None = None
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.engine = FakeEngine(gait_speed)
        self.driver: PvOnlyDriver | None = (driver_cls or FakeDriver)(self.calls) if with_driver else None
        self.wave_speed = 1.9
        self.wave_intro_speed = 2.3
        self.wave_raise_speed = 0.6
        self.dance_speed = 0.0149

    def set_motion(self, name: str) -> None:
        self.calls.append(("set_motion", name))

    def stop(self) -> None:
        self.calls.append(("stop", None))

    def look(self, *, pitch: float, yaw: float) -> None:
        self.calls.append(("look", (pitch, yaw)))

    def step(self) -> dict[str, int]:
        self.calls.append(("step", None))
        return {}


class FailingRobot(FakeRobot):
    """A robot whose `step()` always raises, simulating a servo bus fault."""

    def step(self) -> dict[str, int]:
        raise RuntimeError("servo bus fault")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _no_sleep(_: float) -> None:
    return None


def _robot(fake: FakeRobot) -> Palmimo:
    return cast(Palmimo, fake)


def _loop(robot: FakeRobot, session: PilotSession, *, fps: int = 60) -> RobotLoop:
    return RobotLoop(_robot(robot), session, fps=fps, now=FakeClock(), sleep=_no_sleep)


def test_tick_with_no_pilot_input_stops_the_robot() -> None:
    # Without this, a freshly started server (no pilot connected yet) would
    # command an undefined motion instead of standing still.
    robot = FakeRobot()
    session = PilotSession(now=FakeClock())
    _loop(robot, session).tick()
    assert ("stop", None) in robot.calls
    assert not any(call[0] == "set_motion" for call in robot.calls)


def test_tick_with_pilot_move_calls_set_motion() -> None:
    # Without this, a pilot pressing forward would never actually move the robot.
    robot = FakeRobot()
    clock = FakeClock()
    session = PilotSession(now=clock)
    session.acquire_pilot("a")
    session.update_pilot_input("a", move="forward", rotate=0, neck_pitch=0.0, neck_yaw=0.0)
    _loop(robot, session).tick()
    assert ("set_motion", "forward") in robot.calls


def test_tick_always_applies_look_and_step() -> None:
    # Without this, the neck would never track the pilot's stick, and the
    # engine would never advance a frame -- the robot would just sit there.
    robot = FakeRobot()
    session = PilotSession(now=FakeClock())
    session.acquire_pilot("a")
    session.update_pilot_input("a", move=None, rotate=0, neck_pitch=0.3, neck_yaw=-0.2)
    _loop(robot, session).tick()
    assert ("look", (0.3 * NECK_PITCH_SIGN, -0.2 * NECK_YAW_SIGN)) in robot.calls
    assert ("step", None) in robot.calls


def test_tick_stops_once_the_deadman_expires() -> None:
    # Without this, a robot loop reading a session whose pilot connection
    # died would keep issuing the last motion forever instead of stopping.
    from palmimo_teleop.session import DEADMAN_TIMEOUT_S

    robot = FakeRobot()
    clock = FakeClock()
    session = PilotSession(now=clock)
    session.acquire_pilot("a")
    session.update_pilot_input("a", move="forward", rotate=0, neck_pitch=0.0, neck_yaw=0.0)
    clock.now += DEADMAN_TIMEOUT_S + 0.01

    _loop(robot, session).tick()
    assert ("stop", None) in robot.calls
    assert not any(call[0] == "set_motion" for call in robot.calls)


def test_tick_does_not_reissue_set_motion_while_the_resolved_motion_is_unchanged() -> None:
    # Without this, `set_motion("dance")`/`"wave"` would be called every
    # tick, and `MotionEngine` resets a WAVE/DANCE motion's phase on every
    # (re)selection -- a playing gesture would restart from frame 0 forever
    # instead of ever playing.
    robot = FakeRobot()
    session = PilotSession(now=FakeClock())
    session.acquire_pilot("a")
    session.update_pilot_input("a", move=None, rotate=0, neck_pitch=0.0, neck_yaw=0.0)
    session.play("a", "dance")
    loop = _loop(robot, session)
    loop.tick()
    loop.tick()
    loop.tick()
    assert [call for call in robot.calls if call[0] == "set_motion"] == [("set_motion", "dance")]


def test_tick_reissues_set_motion_when_the_resolved_motion_changes() -> None:
    # Without this, switching from one gesture/gait to another (e.g. wave
    # playing, dance tapped) would never reach the robot after the first
    # motion was applied.
    robot = FakeRobot()
    session = PilotSession(now=FakeClock())
    session.acquire_pilot("a")
    session.update_pilot_input("a", move=None, rotate=0, neck_pitch=0.0, neck_yaw=0.0)
    session.play("a", "wave")
    loop = _loop(robot, session)
    loop.tick()
    session.play("a", "dance")
    loop.tick()
    assert [call for call in robot.calls if call[0] == "set_motion"] == [
        ("set_motion", "wave"),
        ("set_motion", "dance"),
    ]


def test_tick_stops_once_when_a_playing_gesture_is_toggled_off() -> None:
    # Without this, toggling off a playing gesture would either never call
    # `robot.stop()` (edge-triggering swallowing the None transition too) or
    # call it every subsequent tick instead of exactly once.
    robot = FakeRobot()
    session = PilotSession(now=FakeClock())
    session.acquire_pilot("a")
    session.update_pilot_input("a", move=None, rotate=0, neck_pitch=0.0, neck_yaw=0.0)
    session.play("a", "dance")
    loop = _loop(robot, session)
    loop.tick()
    assert [call for call in robot.calls if call[0] == "set_motion"] == [("set_motion", "dance")]
    session.play("a", "dance")  # re-tap the active gesture: early stop
    loop.tick()
    loop.tick()
    assert [call for call in robot.calls if call[0] == "stop"] == [("stop", None)]


def test_shutdown_stops_and_settles_the_robot() -> None:
    # Without this, Ctrl-C/SIGTERM shutdown could leave a mid-stride robot
    # cut off abruptly instead of easing to idle.
    robot = FakeRobot()
    session = PilotSession(now=FakeClock())
    loop = _loop(robot, session, fps=10)
    assert loop.shutdown() is True
    assert robot.calls[0] == ("stop", None)
    step_calls = [call for call in robot.calls if call[0] == "step"]
    assert len(step_calls) == 10  # fps=10 * SETTLE_SECONDS=1.0


def test_shutdown_without_start_still_stops_the_robot() -> None:
    # Without this, shutting down a loop that was never started (e.g.
    # startup failed before `start()`) could crash instead of safely no-op'ing.
    robot = FakeRobot()
    session = PilotSession(now=FakeClock())
    loop = _loop(robot, session)
    loop.shutdown()
    assert ("stop", None) in robot.calls


def test_shutdown_skips_settle_when_the_thread_does_not_stop_in_time() -> None:
    # Without this, `shutdown()` would run `_settle()` (which calls into
    # `robot`) while a still-alive `_run()` thread could also be calling into
    # `robot` -- breaking the "only one thread ever touches the robot" invariant.
    robot = FakeRobot()
    session = PilotSession(now=FakeClock())
    loop = RobotLoop(_robot(robot), session, now=FakeClock(), sleep=_no_sleep, settle_timeout=0.05)
    loop._thread = threading.Thread(target=lambda: time.sleep(1.0), daemon=True)
    loop._thread.start()
    assert loop.shutdown() is False
    assert robot.calls == []


def test_start_refuses_a_second_thread_after_a_timed_out_shutdown() -> None:
    # Without this, a servo bus hang that outlives shutdown()'s settle_timeout
    # would let the next start() spin up a second thread driving the same
    # robot concurrently -- the shutdown-timeout branch used to clear the
    # thread handle, leaving `start()` no way to tell the old thread was
    # still alive.
    robot = FakeRobot()
    session = PilotSession(now=FakeClock())
    loop = RobotLoop(_robot(robot), session, now=FakeClock(), sleep=_no_sleep, settle_timeout=0.05)
    stuck_thread = threading.Thread(target=lambda: time.sleep(1.0), name="palmimo-teleop-robot-loop", daemon=True)
    loop._thread = stuck_thread
    stuck_thread.start()
    assert loop.shutdown() is False

    loop.start()

    running = [t for t in threading.enumerate() if t.name == "palmimo-teleop-robot-loop"]
    assert running == [stuck_thread]
    stuck_thread.join(timeout=2.0)


def test_tick_failure_sets_robot_ok_false() -> None:
    # Without this, a servo bus fault would have no visible signal anywhere
    # in the app -- the status broadcast would keep reporting everything fine.
    robot = FailingRobot()
    session = PilotSession(now=FakeClock())
    loop = _loop(robot, session)
    assert loop.tick() is False
    assert loop.robot_ok is False


def test_tick_recovering_after_a_failure_sets_robot_ok_true_again() -> None:
    # Without this, one transient bad tick would permanently wedge
    # `robot_ok` at False even after the robot recovered.
    session = PilotSession(now=FakeClock())
    loop = _loop(FailingRobot(), session)
    loop.tick()
    loop.robot = _robot(FakeRobot())
    assert loop.tick() is True
    assert loop.robot_ok is True


def test_tick_failure_logging_is_throttled(caplog: pytest.LogCaptureFixture) -> None:
    # Without this, a persistently faulting servo bus would log one line per
    # control cycle (up to `fps`/s) instead of roughly once per FAILURE_LOG_INTERVAL_S.
    import logging

    clock = FakeClock()
    loop = RobotLoop(_robot(FailingRobot()), PilotSession(now=clock), now=clock, sleep=_no_sleep)
    with caplog.at_level(logging.ERROR, logger="palmimo_teleop.robot_loop"):
        loop.tick()  # logged immediately (first failure)
        clock.now += FAILURE_LOG_INTERVAL_S / 2
        loop.tick()  # inside the throttle window -- not logged
        clock.now += FAILURE_LOG_INTERVAL_S
        loop.tick()  # past the window -- logged again
    assert len(caplog.records) == 2


def test_loop_fps_excludes_failed_ticks() -> None:
    # Without this, a servo bus failing most cycles would still report a
    # near-target fps instead of reflecting how few ticks actually succeeded.
    clock = FakeClock()
    loop = RobotLoop(_robot(FailingRobot()), PilotSession(now=clock), fps=10, now=clock, sleep=_no_sleep)
    loop._window_start = 0.0
    for _ in range(5):
        loop._update_loop_fps(loop.tick(), clock.now)
        clock.now += 0.1
    clock.now = 1.1
    loop._update_loop_fps(loop.tick(), clock.now)
    assert loop.loop_fps == 0.0


def _speeds(robot: FakeRobot) -> tuple[float, float, float, float, float]:
    return (
        robot.engine.gait_speed,
        robot.wave_speed,
        robot.wave_intro_speed,
        robot.wave_raise_speed,
        robot.dance_speed,
    )


def _piloted_session(clock: FakeClock) -> PilotSession:
    session = PilotSession(now=clock)
    session.acquire_pilot("a")
    session.update_pilot_input("a", move=None, rotate=0, neck_pitch=0.0, neck_yaw=0.0)
    return session


def test_tick_scales_only_the_gait_speed_by_the_selected_multiplier() -> None:
    # Without this, the pilot's speed buttons would change nothing on the robot,
    # or would also speed up wave and dance, which always play at their own speed.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 2.0)
    loop.tick()
    assert _speeds(robot) == pytest.approx((0.024, 1.9, 2.3, 0.6, 0.0149))


def test_tick_scales_from_the_startup_speeds_rather_than_compounding() -> None:
    # Without this, stepping 1x -> 2x -> 3x would multiply an already doubled
    # value and leave the robot at 6x.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    for speed in (2.0, 3.0, 1.0):
        session.set_speed("a", speed)
        loop.tick()
    assert _speeds(robot) == pytest.approx((0.012, 1.9, 2.3, 0.6, 0.0149))


def test_tick_caps_the_multiplied_gait_speed_at_the_cli_maximum() -> None:
    # Without this, a robot started with a fast --gait-speed would walk at a
    # gait speed the CLI itself refuses once the pilot picks 3x.
    robot = FakeRobot(gait_speed=0.03)
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 3.0)
    loop.tick()
    assert robot.engine.gait_speed == MAX_GAIT_SPEED


def test_tick_returns_to_the_startup_speeds_when_the_deadman_expires() -> None:
    # Without this, a pilot whose connection drops would leave the next pilot
    # (or an unattended robot) at whatever multiplier was last selected.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 3.0)
    loop.tick()
    clock.now = 1.0
    loop.tick()
    assert _speeds(robot) == pytest.approx((0.012, 1.9, 2.3, 0.6, 0.0149))


def test_shutdown_settles_at_the_startup_speeds() -> None:
    # Without this, a robot stopped while at 3x would ease to neutral at 3x
    # instead of at its own tuned speed.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 3.0)
    loop.tick()
    loop.shutdown()
    assert _speeds(robot) == pytest.approx((0.012, 1.9, 2.3, 0.6, 0.0149))


def _profile_writes(robot: FakeRobot) -> list[Any]:
    return [args for name, args in robot.calls if name == "profile_velocity"]


def _drive(session: PilotSession, *, move: str | None = None, rotate: int = 0) -> None:
    session.update_pilot_input("a", move=move, rotate=rotate, neck_pitch=0.0, neck_yaw=0.0)


def test_tick_shortens_the_leg_profile_with_the_gait_speed() -> None:
    # Without this, the leg servos' fixed-time profile would low-pass a 3x gait
    # into a small, fast stride that barely covers more ground than 1x.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 3.0)
    _drive(session, move="forward")
    loop.tick()
    assert _profile_writes(robot)[-1] == (100, LEG_MOTORS)


def test_tick_writes_the_leg_profile_after_step() -> None:
    # Without this, `Palmimo.step()` restoring the driver default as a gesture
    # ends would overwrite the gait's shortened profile on that same tick.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 2.0)
    _drive(session, rotate=1)
    loop.tick()
    names = [name for name, _ in robot.calls]
    assert names.index("step") < len(names) - 1 - names[::-1].index("profile_velocity")


def test_tick_restores_the_default_leg_profile_when_standing() -> None:
    # Without this, a robot that stopped walking at 3x would hold every later
    # pose change at the shortened, snappier profile.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 3.0)
    _drive(session, move="forward")
    loop.tick()
    _drive(session)
    loop.tick()
    assert _profile_writes(robot)[-1] == (300, LEG_MOTORS)


def test_tick_leaves_the_servo_profile_alone_during_a_gesture() -> None:
    # Without this, the loop would overwrite the crisp arm profile `Palmimo`
    # sets for the wave.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    loop.tick()
    before = len(_profile_writes(robot))
    session.set_speed("a", 3.0)
    session.play("a", "wave")
    loop.tick()
    loop.tick()
    assert len(_profile_writes(robot)) == before


def test_tick_rewrites_the_leg_profile_after_a_gesture() -> None:
    # Without this, a gait resumed after a wave would keep the default profile
    # `Palmimo` restored when the wave ended, losing the 3x stride.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 3.0)
    _drive(session, move="forward")
    loop.tick()
    session.play("a", "wave")
    _drive(session)
    loop.tick()
    _drive(session, move="forward")
    loop.tick()
    assert _profile_writes(robot)[-1] == (100, LEG_MOTORS)


def test_tick_matches_the_leg_profile_to_a_capped_gait_speed() -> None:
    # Without this, a gait speed held at the cap would get a profile shortened
    # by the full multiplier, faster than the commands it is tracking.
    robot = FakeRobot(gait_speed=0.03)
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 3.0)
    _drive(session, move="forward")
    loop.tick()
    assert _profile_writes(robot)[-1] == (round(300 * 0.03 / MAX_GAIT_SPEED), LEG_MOTORS)


def test_tick_skips_the_leg_profile_without_a_driver() -> None:
    # Without this, a compute-only run (no servo driver) would fail every tick.
    robot = FakeRobot(with_driver=False)
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 3.0)
    _drive(session, move="forward")
    assert loop.tick() is True


def test_shutdown_restores_the_default_leg_profile() -> None:
    # Without this, a robot shut down mid-walk at 3x would settle with the
    # shortened profile still on its leg servos.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 3.0)
    _drive(session, move="forward")
    loop.tick()
    loop.shutdown()
    assert _profile_writes(robot)[-1] == (300, LEG_MOTORS)


def test_switching_from_a_fast_gait_to_dance_restores_the_leg_profile_before_step() -> None:
    # Without this, a dance after walking at 3x would run with the shortened
    # leg profile, since `Palmimo` applies no servo tuning of its own to a dance.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 3.0)
    _drive(session, move="forward")
    loop.tick()
    robot.calls.clear()
    session.play("a", "dance")
    _drive(session)
    loop.tick()
    names = [name for name, _ in robot.calls]
    assert _profile_writes(robot) == [(300, LEG_MOTORS)]
    assert names.index("profile_velocity") < names.index("step")


def test_shutdown_during_a_dance_leaves_no_shortened_leg_profile() -> None:
    # Without this, a server stopped mid-dance after a 3x walk would settle and
    # park with the shortened profile still on the legs.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 3.0)
    _drive(session, move="forward")
    loop.tick()
    session.play("a", "dance")
    _drive(session)
    loop.tick()
    loop.shutdown()
    assert _profile_writes(robot)[-1] == (300, LEG_MOTORS)


def test_a_failed_leg_profile_write_does_not_fail_the_cycle_and_is_retried() -> None:
    # Without this, one dropped bus reply would flash the pilot's servo-fault
    # banner and, if never retried, leave the gait on the slow profile.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 3.0)
    _drive(session, move="forward")
    assert robot.driver is not None
    robot.driver.fail_next_writes = 1
    assert loop.tick() is True
    assert loop.robot_ok is True
    assert _profile_writes(robot) == []
    loop._now = lambda: LEG_PROFILE_RETRY_BASE_S  # first back-off elapsed
    loop.tick()
    assert _profile_writes(robot) == [(100, LEG_MOTORS)]


def test_a_gesture_is_not_written_to_again_after_its_first_cycle() -> None:
    # Without this, a retry during a wave would overwrite the crisp arm profile
    # `Palmimo` applied on the wave's first step.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 3.0)
    _drive(session, move="forward")
    loop.tick()
    assert robot.driver is not None
    robot.driver.fail_next_writes = 1
    session.play("a", "wave")
    _drive(session)
    loop.tick()
    robot.calls.clear()
    loop.tick()
    loop.tick()
    assert _profile_writes(robot) == []


# -- Leg Profile_Velocity: partial writes, dirty legs, back-off, settle ------------------------------


def _clocked(robot: FakeRobot) -> tuple[RobotLoop, PilotSession, FakeClock]:
    """A loop and a piloted session sharing one fake clock, for back-off timing."""
    clock = FakeClock()
    session = _piloted_session(clock)
    return RobotLoop(_robot(robot), session, fps=60, now=clock, sleep=_no_sleep), session, clock


def _pv(robot: FakeRobot) -> PvOnlyDriver:
    assert robot.driver is not None
    return robot.driver


def _all_legs_at(robot: FakeRobot, value: int) -> bool:
    return all(v == value for v in _pv(robot).pv.values())


def _walk_fast_with_a_partial_failure(robot: FakeRobot) -> tuple[RobotLoop, PilotSession, FakeClock]:
    """Walk at 2.5x with the first leg profile write failing after 7 of the 18 legs."""
    loop, session, clock = _clocked(robot)
    session.set_speed("a", 2.5)
    _drive(session, move="forward")
    _pv(robot).fail_after = 7
    loop.tick()
    assert sum(v != 300 for v in _pv(robot).pv.values()) == 7  # mixed legs on the "hardware"
    return loop, session, clock


def test_a_partial_leg_profile_failure_is_completed_while_walking() -> None:
    # Without this, the 11 legs the failed write never reached would stay at the
    # default while the other 7 run short, a lopsided gait.
    robot = FakeRobot()
    loop, session, clock = _walk_fast_with_a_partial_failure(robot)
    clock.now = LEG_PROFILE_RETRY_BASE_S
    _drive(session, move="forward")
    loop.tick()
    assert _all_legs_at(robot, 120)


def test_stopping_after_a_partial_failure_restores_every_leg() -> None:
    # Without this, standing still after a partly failed fast-gait write would
    # hold some legs at the shortened profile.
    robot = FakeRobot()
    loop, session, _ = _walk_fast_with_a_partial_failure(robot)
    _drive(session)
    loop.tick()  # motion changed: retried at once, despite the back-off
    assert _all_legs_at(robot, 300)


def test_returning_to_1x_after_a_partial_failure_restores_every_leg() -> None:
    # Without this, dropping to 1x mid-walk after a failed write would keep the
    # legs it reached shortened while the gait runs at 1x.
    robot = FakeRobot()
    loop, session, _ = _walk_fast_with_a_partial_failure(robot)
    session.set_speed("a", 1.0)
    _drive(session, move="forward")
    loop.tick()  # speed changed: retried at once
    assert _all_legs_at(robot, 300)


def test_a_partial_failure_before_a_dance_is_retried_during_the_dance() -> None:
    # Without this, one failed restore at the switch would leave the whole dance
    # on the shortened leg profile, since `Palmimo` never retunes a dance.
    robot = FakeRobot()
    loop, session, clock = _clocked(robot)
    session.set_speed("a", 2.5)
    _drive(session, move="forward")
    loop.tick()
    assert _all_legs_at(robot, 120)
    _pv(robot).fail_after = 5
    session.play("a", "dance")
    _drive(session)
    loop.tick()
    assert not _all_legs_at(robot, 300)
    clock.now = LEG_PROFILE_RETRY_BASE_S / 2
    _drive(session)
    loop.tick()
    assert not _all_legs_at(robot, 300)  # back-off not yet elapsed: no write
    clock.now = LEG_PROFILE_RETRY_BASE_S
    _drive(session)
    loop.tick()
    assert _all_legs_at(robot, 300)
    assert session.effective_input().gesture == "dance"


def test_a_failed_restore_before_a_wave_is_not_retried_during_the_wave() -> None:
    # Without this, a retry mid-wave would overwrite the crisp arm profile
    # `Palmimo` applied on the wave's first step -- even when a speed tap
    # mid-wave changes the input.
    robot = FakeRobot()
    loop, session, clock = _clocked(robot)
    session.set_speed("a", 2.5)
    _drive(session, move="forward")
    loop.tick()
    _pv(robot).fail_after = 5
    session.play("a", "wave")
    _drive(session)
    loop.tick()
    attempts = _pv(robot).write_attempts
    for i in range(1, 30):
        clock.now = i * LEG_PROFILE_RETRY_MAX_S / 10
        if i == 10:
            session.set_speed("a", 1.0)
        _drive(session)
        loop.tick()
    assert session.effective_input().gesture == "wave"
    assert _pv(robot).write_attempts == attempts


def test_every_leg_is_rewritten_after_a_wave_without_trusting_palmimo() -> None:
    # Without this, a wave whose own servo tuning failed silently (Palmimo
    # suppresses those errors) would leave shortened legs behind it.
    robot = FakeRobot()
    loop, session, _ = _clocked(robot)
    session.set_speed("a", 2.5)
    _drive(session, move="forward")
    loop.tick()
    _pv(robot).fail_after = 5
    session.play("a", "wave")
    _drive(session)
    loop.tick()
    session.play("a", "wave")  # tap again: stops the wave
    _drive(session)
    loop.tick()
    assert _all_legs_at(robot, 300)


def test_a_wave_on_a_driver_palmimo_does_not_tune_is_restored_like_a_dance() -> None:
    # Without this, on a PV-only driver (no gain/IIR, so `Palmimo` applies no
    # wave tuning) a failed restore would never be retried during the wave.
    robot = FakeRobot(driver_cls=PvOnlyDriver)
    loop, session, clock = _clocked(robot)
    session.set_speed("a", 2.5)
    _drive(session, move="forward")
    loop.tick()
    _pv(robot).fail_after = 5
    session.play("a", "wave")
    _drive(session)
    loop.tick()
    clock.now = LEG_PROFILE_RETRY_BASE_S
    _drive(session)
    loop.tick()
    assert _all_legs_at(robot, 300)


def test_continued_leg_profile_failures_back_off_up_to_the_cap() -> None:
    # Without this, a misbehaving bus would cost every 60 fps cycle a full
    # 18-motor write and its timeouts.
    robot = FakeRobot()
    loop, session, clock = _clocked(robot)
    session.set_speed("a", 2.5)
    _pv(robot).always_fail = True
    attempt_times: list[float] = []
    for frame in range(60 * 6):
        clock.now = frame / 60
        _drive(session, move="forward")
        before = _pv(robot).write_attempts
        assert loop.tick() is True
        if _pv(robot).write_attempts != before:
            attempt_times.append(clock.now)
    gaps = [b - a for a, b in itertools.pairwise(attempt_times)]
    assert gaps[0] == pytest.approx(LEG_PROFILE_RETRY_BASE_S, abs=1 / 60)
    assert all(b >= a - 1 / 60 for a, b in itertools.pairwise(gaps))  # non-decreasing
    assert max(gaps) == pytest.approx(LEG_PROFILE_RETRY_MAX_S, abs=1 / 60)
    assert len(attempt_times) < 12  # against 360 cycles
    assert loop.robot_ok is True


def test_a_successful_write_resets_the_back_off() -> None:
    # Without this, one bad spell would leave every later failure waiting the full cap.
    robot = FakeRobot()
    loop, session, clock = _clocked(robot)
    session.set_speed("a", 2.5)
    _pv(robot).fail_next_writes = 3
    for frame in range(60):
        clock.now = frame / 60
        _drive(session, move="forward")
        loop.tick()
    assert _all_legs_at(robot, 120)
    _pv(robot).fail_next_writes = 1
    _drive(session)
    loop.tick()  # stop: fails once
    clock.now += LEG_PROFILE_RETRY_BASE_S
    _drive(session)
    loop.tick()
    assert _all_legs_at(robot, 300)


def test_shutdown_retries_a_failing_leg_restore_within_the_settle() -> None:
    # Without this, a single failed write at shutdown (or a retry spaced at the
    # 5 s log interval) would settle and park on the shortened profile.
    robot = FakeRobot()
    loop, _session, _ = _walk_fast_with_a_partial_failure(robot)
    _pv(robot).fail_next_writes = 2
    assert loop.shutdown() is True
    assert _all_legs_at(robot, 300)


def test_shutdown_gives_up_after_the_settle_attempt_limit_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    # Without this, a dead bus could hang shutdown, or settle could end
    # silently with the legs still shortened.
    robot = FakeRobot()
    loop, session, _ = _clocked(robot)
    session.set_speed("a", 2.5)
    _drive(session, move="forward")
    loop.tick()
    _pv(robot).always_fail = True
    before = _pv(robot).write_attempts
    with caplog.at_level("ERROR"):
        assert loop.shutdown() is True
    assert _pv(robot).write_attempts - before == LEG_PROFILE_SETTLE_ATTEMPTS
    assert any("not restored" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")
    assert ("stop", None) in robot.calls


def test_shutdown_mid_wave_restores_the_legs_only_after_palmimo_releases_the_wave() -> None:
    # Without this, settle would write the legs while the wave still owns the
    # servos, or skip them on the assumption that the wave reset them.
    robot = FakeRobot()
    loop, session, _ = _clocked(robot)
    session.set_speed("a", 2.5)
    _drive(session, move="forward")
    loop.tick()
    session.play("a", "wave")
    _drive(session)
    loop.tick()
    robot.calls.clear()
    loop.shutdown()
    names = [name for name, _ in robot.calls]
    assert names.index("stop") < names.index("step") < names.index("profile_velocity")
    assert _all_legs_at(robot, 300)


def test_shutdown_mid_dance_after_a_failed_restore_restores_every_leg() -> None:
    # Without this, a dance whose pre-dance restore failed would settle and
    # park on the shortened profile.
    robot = FakeRobot()
    loop, session, _ = _clocked(robot)
    session.set_speed("a", 2.5)
    _drive(session, move="forward")
    loop.tick()
    _pv(robot).fail_after = 5
    session.play("a", "dance")
    _drive(session)
    loop.tick()
    assert loop.shutdown() is True
    assert _all_legs_at(robot, 300)


# -- Selected vs applied multiplier ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("gait_speed", "selected", "applied"),
    [(0.012, 2.5, 2.5), (0.012, 1.0, 1.0), (0.03, 2.5, MAX_GAIT_SPEED / 0.03), (0.05, 2.5, 1.0), (0.05, 1.5, 1.0)],
)
def test_applied_multiplier_reports_the_capped_speed(gait_speed: float, selected: float, applied: float) -> None:
    # Without this, a multiplier held back by the gait speed ceiling would be
    # reported as applied.
    loop = _loop(FakeRobot(gait_speed=gait_speed), PilotSession(now=FakeClock()))
    assert loop.applied_multiplier(selected) == pytest.approx(applied)


def _speed_up_with_a_partial_failure_after_1x(robot: FakeRobot) -> tuple[RobotLoop, PilotSession, FakeClock]:
    """Stand at 1x (every leg confirmed at the default), then walk at 2.5x with that write
    failing after 7 legs -- so the loop's last *confirmed* value for every leg is the default."""
    loop, session, clock = _clocked(robot)
    loop.tick()
    assert _all_legs_at(robot, 300)
    session.set_speed("a", 2.5)
    _drive(session, move="forward")
    _pv(robot).fail_after = 7
    loop.tick()
    assert sum(v == 120 for v in _pv(robot).pv.values()) == 7
    return loop, session, clock


def test_stopping_restores_legs_a_failed_write_reached_after_they_were_at_the_default() -> None:
    # Without this, legs last confirmed at the default would be skipped by the
    # restore even though the failed fast-gait write had already shortened them.
    robot = FakeRobot()
    loop, session, _ = _speed_up_with_a_partial_failure_after_1x(robot)
    _drive(session)
    loop.tick()
    assert _all_legs_at(robot, 300)


def test_a_dance_restores_legs_a_failed_write_reached_after_they_were_at_the_default() -> None:
    # Same as above for a dance, which `Palmimo` never retunes.
    robot = FakeRobot()
    loop, session, _ = _speed_up_with_a_partial_failure_after_1x(robot)
    session.play("a", "dance")
    _drive(session)
    loop.tick()
    assert _all_legs_at(robot, 300)


def test_shutdown_restores_legs_a_failed_write_reached_after_they_were_at_the_default() -> None:
    # Same as above for shutdown's settle.
    robot = FakeRobot()
    loop, _session, _ = _speed_up_with_a_partial_failure_after_1x(robot)
    assert loop.shutdown() is True
    assert _all_legs_at(robot, 300)


# -- Back-off overflow, wave -> dance hand-off, settle failures ------------------------------------


def test_a_long_run_of_leg_profile_failures_does_not_overflow_the_back_off() -> None:
    # Without this, `2 ** n` past n = 1023 raises OverflowError inside the
    # failure handler (~17 minutes of 1 s retries against a dead servo): every
    # tick then fails and retries without any back-off at all.
    robot = FakeRobot()
    loop, session, clock = _clocked(robot)
    session.set_speed("a", 2.5)
    _pv(robot).always_fail = True
    loop._leg_pv_failures = 2000
    for frame in range(3):
        clock.now = frame * LEG_PROFILE_RETRY_MAX_S
        _drive(session, move="forward")
        assert loop.tick() is True
    assert loop.robot_ok is True
    assert loop._leg_pv_failures == 2003
    assert loop._leg_pv_next_try_at == pytest.approx(clock.now + LEG_PROFILE_RETRY_MAX_S)


class SdkLikeRobot(FakeRobot):
    """A `FakeRobot` whose `step()` mimics `Palmimo._sync_gesture_tuning` for a wave on its driver.

    On a wave's first step the waving arm (leg 1) goes to `WAVE_ARM_PV`; on the first step after
    the wave every servo goes back to the driver default -- unless `release_fails`, mirroring the
    SDK's error-suppressed release write that silently leaves the arm on the wave profile.
    """

    WAVE_ARM_PV = 0
    ARM = tuple(m for m in LEG_MOTORS if m.startswith("leg_1_"))

    def __init__(self) -> None:
        super().__init__()
        self.motion: str | None = None
        self.release_fails = False
        self._wave_tuned = False

    def set_motion(self, name: str) -> None:
        super().set_motion(name)
        self.motion = name

    def stop(self) -> None:
        super().stop()
        self.motion = None

    def step(self) -> dict[str, int]:
        pv = _pv(self).pv
        if self.motion == "wave" and not self._wave_tuned:
            for motor in self.ARM:
                pv[motor] = self.WAVE_ARM_PV
            self._wave_tuned = True
        elif self.motion != "wave" and self._wave_tuned:
            if not self.release_fails:
                for motor in pv:
                    pv[motor] = 300
            self._wave_tuned = False  # the SDK forgets the wave either way
        return super().step()


def test_a_wave_then_dance_restores_the_arm_when_palmimo_s_release_fails() -> None:
    # Without this, switching a wave straight to a dance after Palmimo's
    # release write silently failed would dance with the arm on the wave's
    # profile until the dance ends: the hand-off had forgotten the legs, so
    # the dance saw nothing dirty to restore.
    robot = SdkLikeRobot()
    loop, session, _ = _clocked(robot)
    loop.tick()
    session.play("a", "wave")
    _drive(session)
    loop.tick()
    assert all(_pv(robot).pv[m] == SdkLikeRobot.WAVE_ARM_PV for m in SdkLikeRobot.ARM)
    robot.release_fails = True
    session.play("a", "dance")
    _drive(session)
    loop.tick()
    assert session.effective_input().gesture == "dance"
    assert _all_legs_at(robot, 300)


def test_a_wave_then_dance_keeps_retrying_the_restore_during_the_dance() -> None:
    # Without this, a restore that also failed at the dance switch would never
    # be retried for the rest of the dance.
    robot = SdkLikeRobot()
    loop, session, clock = _clocked(robot)
    loop.tick()
    session.play("a", "wave")
    _drive(session)
    loop.tick()
    robot.release_fails = True
    _pv(robot).fail_next_writes = 1
    session.play("a", "dance")
    _drive(session)
    loop.tick()
    assert not _all_legs_at(robot, 300)
    clock.now = LEG_PROFILE_RETRY_BASE_S
    _drive(session)
    loop.tick()
    assert _all_legs_at(robot, 300)


def test_the_wave_profile_is_still_not_overwritten_while_the_wave_plays() -> None:
    # Without this, handing the legs over as unknown would let a later wave
    # cycle "restore" them and wipe the arm profile Palmimo applied.
    robot = SdkLikeRobot()
    loop, session, clock = _clocked(robot)
    loop.tick()
    session.play("a", "wave")
    _drive(session)
    loop.tick()
    attempts = _pv(robot).write_attempts
    for i in range(1, 20):
        clock.now = i * LEG_PROFILE_RETRY_MAX_S / 4
        _drive(session)
        loop.tick()
    assert _pv(robot).write_attempts == attempts
    assert all(_pv(robot).pv[m] == SdkLikeRobot.WAVE_ARM_PV for m in SdkLikeRobot.ARM)


class SettleFailingRobot(FakeRobot):
    """A robot whose `step()` starts raising once `fail_steps` is set, simulating a bus lost at shutdown."""

    fail_steps = False

    def step(self) -> dict[str, int]:
        if self.fail_steps:
            raise RuntimeError("servo bus fault")
        return super().step()


def test_a_settle_failure_raises_settle_failed_instead_of_reporting_success() -> None:
    # Without this, a bus failing mid-settle would either escape as a bare
    # error the server cannot tell apart from anything else, or be swallowed
    # and reported as a clean shutdown.
    robot = SettleFailingRobot()
    loop, session, _ = _clocked(robot)
    _drive(session, move="forward")
    loop.tick()
    robot.fail_steps = True
    with pytest.raises(SettleFailedError) as excinfo:
        loop.shutdown()
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert ("stop", None) in robot.calls


def test_a_settle_failure_after_the_thread_stops_still_raises_settle_failed() -> None:
    # Same, through the real background thread: `shutdown()` joins it first,
    # so the error is a settle failure, not a stuck-thread `False`.
    robot = SettleFailingRobot()
    session = PilotSession(now=FakeClock())
    loop = RobotLoop(_robot(robot), session, fps=200)
    loop.start()
    time.sleep(0.05)
    robot.fail_steps = True
    with pytest.raises(SettleFailedError):
        loop.shutdown()
