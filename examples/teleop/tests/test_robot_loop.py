"""Behavior of `palmimo_teleop.robot_loop.RobotLoop`, driven deterministically via `tick()`
and `shutdown()` -- never through the real background thread or real time."""

import threading
import time
from typing import Any, cast

import pytest

from palmimo_sdk import Palmimo
from palmimo_teleop.control import LEG_MOTORS, MAX_GAIT_SPEED
from palmimo_teleop.robot_loop import FAILURE_LOG_INTERVAL_S, NECK_PITCH_SIGN, NECK_YAW_SIGN, RobotLoop
from palmimo_teleop.session import PilotSession


class FakeEngine:
    def __init__(self, gait_speed: float) -> None:
        self.gait_speed = gait_speed
        self.step_height = 30.0


class FakeDriver:
    """A time-based-profile servo driver; profile writes land in the robot's call log."""

    profile_velocity = 300

    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self._calls = calls

    fail_next_writes = 0

    def set_profile_velocity_units(self, value: int, motors: list[str] | None = None) -> None:
        if self.fail_next_writes:
            self.fail_next_writes -= 1
            raise ConnectionError("no status packet")
        self._calls.append(("profile_velocity", (value, tuple(motors) if motors else None)))


class FakeRobot:
    """Records every call `RobotLoop` makes, in order, and holds the speed knobs it writes."""

    def __init__(self, *, gait_speed: float = 0.012, with_driver: bool = True) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.engine = FakeEngine(gait_speed)
        self.driver = FakeDriver(self.calls) if with_driver else None
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


def test_tick_scales_every_speed_knob_by_the_selected_multiplier() -> None:
    # Without this, the pilot's speed buttons would change nothing on the robot,
    # or would speed up walking but leave wave and dance at their own rate.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 2.0)
    loop.tick()
    assert _speeds(robot) == pytest.approx((0.024, 3.8, 4.6, 1.2, 0.0298))


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


def test_four_x_lowers_the_gait_lift_and_three_x_restores_it() -> None:
    # Without this, 4x would drive the lift joints past what the servos give
    # comfortably, or a lowered lift would stick after stepping back down.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 4.0)
    loop.tick()
    assert robot.engine.step_height == pytest.approx(18.0)
    assert robot.engine.gait_speed == pytest.approx(0.048)
    session.set_speed("a", 3.0)
    loop.tick()
    assert robot.engine.step_height == pytest.approx(30.0)


def test_four_x_plays_gestures_at_three_x() -> None:
    # Without this, selecting 4x for walking would also run wave and dance at
    # a speed they have never been run at on hardware.
    robot = FakeRobot()
    clock = FakeClock()
    session = _piloted_session(clock)
    loop = _loop(robot, session)
    session.set_speed("a", 4.0)
    loop.tick()
    assert (robot.wave_speed, robot.wave_intro_speed, robot.wave_raise_speed, robot.dance_speed) == pytest.approx(
        (5.7, 6.9, 1.8, 0.0447)
    )
