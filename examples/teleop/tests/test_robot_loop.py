"""Behavior of `palmimo_teleop.robot_loop.RobotLoop`, driven deterministically via `tick()`
and `shutdown()` -- never through the real background thread or real time."""

import threading
import time
from typing import Any

import pytest

from palmimo_teleop.robot_loop import FAILURE_LOG_INTERVAL_S, NECK_PITCH_SIGN, NECK_YAW_SIGN, RobotLoop
from palmimo_teleop.session import PilotSession


class FakeRobot:
    """Records every call `RobotLoop` makes, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

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


def _loop(robot: FakeRobot, session: PilotSession, *, fps: int = 60) -> RobotLoop:
    return RobotLoop(robot, session, fps=fps, now=FakeClock(), sleep=_no_sleep)


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
    loop = RobotLoop(robot, session, now=FakeClock(), sleep=_no_sleep, settle_timeout=0.05)
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
    loop = RobotLoop(robot, session, now=FakeClock(), sleep=_no_sleep, settle_timeout=0.05)
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
    loop.robot = FakeRobot()
    assert loop.tick() is True
    assert loop.robot_ok is True


def test_tick_failure_logging_is_throttled(caplog: pytest.LogCaptureFixture) -> None:
    # Without this, a persistently faulting servo bus would log one line per
    # control cycle (up to `fps`/s) instead of roughly once per FAILURE_LOG_INTERVAL_S.
    import logging

    clock = FakeClock()
    loop = RobotLoop(FailingRobot(), PilotSession(now=clock), now=clock, sleep=_no_sleep)
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
    loop = RobotLoop(FailingRobot(), PilotSession(now=clock), fps=10, now=clock, sleep=_no_sleep)
    loop._window_start = 0.0
    for _ in range(5):
        loop._update_loop_fps(loop.tick(), clock.now)
        clock.now += 0.1
    clock.now = 1.1
    loop._update_loop_fps(loop.tick(), clock.now)
    assert loop.loop_fps == 0.0
