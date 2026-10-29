import json
import logging
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

import palmimo_sdk.robot as robot_module
from palmimo_sdk import HardwareErrorRead, Palmimo
from palmimo_sdk.engine import MotionEngine
from palmimo_sdk.kinematics import LEG_MOTORS
from palmimo_sdk.thermal import NECK_MOTORS

from .flight_fakes import Clock, FlightDriver


@pytest.fixture(autouse=True)
def skip_glide_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(robot_module.time, "sleep", lambda _: None)


@pytest.fixture
def rig(tmp_path: Path) -> tuple[Palmimo, FlightDriver, Clock]:
    clock = Clock()
    driver = FlightDriver(clock, tmp_path / "incidents")
    robot = Palmimo(driver=driver, auto_wake=False, thermal_clock=clock, incident_dir=driver.directory)
    robot.connect()
    return robot, driver, clock


def step(robot: Palmimo, clock: Clock, now: float) -> dict[str, int]:
    clock.now = now
    return robot.step()


def latch(robot: Palmimo, driver: FlightDriver, clock: Clock) -> None:
    step(robot, clock, 0)
    driver.bits[LEG_MOTORS[0]] = 32
    step(robot, clock, 1)
    step(robot, clock, 1.01)


def rows(robot: Palmimo) -> list[dict[str, Any]]:
    path = robot.last_incident_path
    assert path is not None
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_robot_records_incident_from_leg_sweep_through_post_window(rig: tuple[Palmimo, FlightDriver, Clock]) -> None:
    robot, driver, clock = rig
    robot.forward()
    latch(robot, driver, clock)
    assert robot.last_incident_path is None
    step(robot, clock, 2)
    step(robot, clock, 3.02)
    header, *samples = rows(robot)
    assert header["trigger_bits"] == {LEG_MOTORS[0]: 32}
    assert header["truncated"] is False
    assert any(row["tier"] == "fast" and row["t"] < 0 for row in samples)
    assert samples[-1]["t"] >= 2
    assert all(not exists for event, _, exists in driver.events if event == "write")


def test_robot_records_leg_sweep_without_extra_bus_reads(tmp_path: Path) -> None:
    reads = []
    for enabled in (False, True):
        clock = Clock()
        driver = FlightDriver(clock, tmp_path / str(enabled))
        robot = Palmimo(driver=driver, auto_wake=False, thermal_clock=clock, flight_recorder=enabled)
        robot.connect()
        for frame in range(150):
            step(robot, clock, frame / 60)
        reads.append(driver.reads)
    assert reads[0] == reads[1]


@pytest.mark.parametrize("fps", [30, 60])
def test_robot_reads_hardware_errors_within_three_frames_and_prefers_no_leg_sweep(tmp_path: Path, fps: int) -> None:
    clock = Clock()
    driver = FlightDriver(clock, tmp_path / "incidents")
    robot = Palmimo(driver=driver, auto_wake=False, thermal_clock=clock, fps=fps)
    robot.connect()
    for frame in range(round(fps * 4.4)):
        step(robot, clock, (frame + 0.37) / fps)
    polls = [now for event, now, _ in driver.events if event == "errors"]
    assert len(polls) == 5
    assert all(1 <= second - first <= 1 + 3 / fps + 1e-9 for first, second in pairwise(polls))
    if fps == 60:
        sweeps = {now for now, names in driver.reads if names == LEG_MOTORS}
        assert all(now not in sweeps for now in polls[1:])


def test_robot_reads_hardware_errors_in_connect_before_wake(tmp_path: Path) -> None:
    clock = Clock()
    driver = FlightDriver(clock, tmp_path / "incidents")
    robot = Palmimo(driver=driver, thermal_clock=clock)
    robot.connect()
    events = [event for event, _, _ in driver.events]
    assert events.index("connect") < events.index("errors") < events.index("write")


def test_robot_records_latch_during_wake_as_new_incident(tmp_path: Path) -> None:
    clock = Clock()
    driver = FlightDriver(clock, tmp_path / "incidents")
    driver.after_write = lambda: driver.bits.update({LEG_MOTORS[0]: 32})
    robot = Palmimo(driver=driver, thermal_clock=clock, incident_dir=driver.directory)
    robot.connect()
    wake_goal = driver.writes[-1]
    step(robot, clock, 2)
    step(robot, clock, 2.01)
    step(robot, clock, 4.02)
    recorded = rows(robot)
    assert recorded[0]["trigger_bits"] == {LEG_MOTORS[0]: 32}
    assert any(row["tier"] == "fast" and row["motion"] == "wake" for row in recorded[1:])
    first_fast = next(row for row in recorded[1:] if row["tier"] == "fast" and row["motion"] != "wake")
    assert {name: sample["goal"] for name, sample in first_fast["axes"].items()} == {
        name: wake_goal[name] for name in LEG_MOTORS
    }


def test_robot_records_last_written_goal_not_engine_output(rig: tuple[Palmimo, FlightDriver, Clock]) -> None:
    robot, driver, clock = rig
    robot.forward()
    previous = step(robot, clock, 0)
    current = step(robot, clock, 0.1)
    assert previous != current
    driver.bits[LEG_MOTORS[0]] = 32
    step(robot, clock, 1)
    step(robot, clock, 1.01)
    robot.flush_incident()
    sample = next(row for row in rows(robot)[1:] if row["tier"] == "fast" and row["t"] == pytest.approx(-0.91))
    assert {name: axis["goal"] for name, axis in sample["axes"].items()} == {
        name: previous[name] for name in LEG_MOTORS
    }


def test_robot_records_neutral_backfill_after_partial_write(
    rig: tuple[Palmimo, FlightDriver, Clock], monkeypatch: pytest.MonkeyPatch
) -> None:
    robot, driver, clock = rig
    latch(robot, driver, clock)
    captured = False

    def capture_during_hold(_: float) -> None:
        nonlocal captured
        if not captured and set(driver.writes[-1]) == set(LEG_MOTORS):
            captured = True
            step(robot, clock, 2.1)

    monkeypatch.setattr(robot_module.time, "sleep", capture_during_hold)
    monkeypatch.setattr(robot_module.time, "perf_counter", clock)
    robot.perform_dance(sways=0, end_hold=0.001, settle=0)
    assert captured
    robot.flush_incident()
    sample = next(row for row in rows(robot)[1:] if row["tier"] == "context" and row["t"] > 1)
    assert all(sample["axes"][name]["goal"] == MotionEngine.NEUTRAL for name in NECK_MOTORS)


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("park", [False, True])
def test_robot_disconnect_flushes_after_torque_off_even_on_driver_error(
    rig: tuple[Palmimo, FlightDriver, Clock], fails: bool, park: bool
) -> None:
    robot, driver, clock = rig
    latch(robot, driver, clock)
    driver.disconnect_failure = fails
    if park:
        clock.now = 3.02
    if fails:
        with pytest.raises(OSError):
            robot.disconnect(park=park)
    else:
        robot.disconnect(park=park)
    assert rows(robot)[0]["truncated"] is (not park)
    assert [exists for event, _, exists in driver.events if event == "disconnect"] == [False]


@pytest.mark.parametrize("failure", [NotImplementedError(), KeyError("unknown")])
def test_robot_unsupported_hardware_error_read_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, failure: Exception
) -> None:
    clock = Clock()
    driver = FlightDriver(clock, tmp_path / "incidents")
    driver.read_failure = failure
    robot = Palmimo(driver=driver, auto_wake=False, thermal_clock=clock)
    robot.connect()
    for now in range(10):
        step(robot, clock, now)
    assert len([event for event, _, _ in driver.events if event == "errors"]) == 1
    assert (
        len(
            [
                record
                for record in caplog.records
                if record.name == "palmimo_sdk.robot" and record.levelno == logging.WARNING
            ]
        )
        == 1
    )


@pytest.mark.parametrize("failure", [OSError("bus lost"), RuntimeError("bus failed")])
def test_robot_hardware_error_read_failure_does_not_stop_steps_and_logs_at_most_every_five_seconds(
    rig: tuple[Palmimo, FlightDriver, Clock], caplog: pytest.LogCaptureFixture, failure: Exception
) -> None:
    robot, driver, clock = rig
    driver.read_failure = failure
    for frame in range(600):
        step(robot, clock, frame / 60)
    assert len(driver.writes) == 600
    assert (
        len(
            [
                record
                for record in caplog.records
                if record.name == "palmimo_sdk.robot" and record.levelno == logging.WARNING
            ]
        )
        == 2
    )


def test_robot_flight_recorder_false_reads_no_hardware_errors(tmp_path: Path) -> None:
    clock = Clock()
    driver = FlightDriver(clock, tmp_path / "incidents")
    robot = Palmimo(
        driver=driver, flight_recorder=False, incident_dir=driver.directory, auto_wake=False, thermal_clock=clock
    )
    robot.connect()
    for now in range(5):
        step(robot, clock, now)
    assert all(event != "errors" for event, _, _ in driver.events)
    assert robot.incident_dir is None
    assert robot.flush_incident() is None


def test_robot_connect_resolves_xdg_without_creating_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    driver = FlightDriver(clock, tmp_path / "unused")
    robot = Palmimo(driver=driver, auto_wake=False, thermal_clock=clock)
    assert robot.incident_dir is None
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    robot.connect()
    assert robot.incident_dir == tmp_path / "state/palmimo/incidents"
    assert not list(tmp_path.iterdir())


def test_robot_explicit_incident_dir_is_known_before_connect(tmp_path: Path) -> None:
    robot = Palmimo(incident_dir=tmp_path)
    assert robot.incident_dir == tmp_path


def test_robot_flush_returns_path_only_when_written_and_preserves_it_across_reconnect(
    rig: tuple[Palmimo, FlightDriver, Clock],
) -> None:
    robot, driver, clock = rig
    latch(robot, driver, clock)
    path = robot.flush_incident()
    assert path is not None and path == robot.last_incident_path
    assert robot.flush_incident() is None
    robot.disconnect(park=False)
    robot.connect()
    assert robot.last_incident_path == path
    assert robot.flush_incident() is None


@pytest.mark.parametrize("neck_fails", [False, True])
def test_robot_context_includes_only_fresh_neck_sweeps(
    rig: tuple[Palmimo, FlightDriver, Clock], neck_fails: bool
) -> None:
    robot, driver, clock = rig
    latch(robot, driver, clock)
    driver.neck_failure = neck_fails
    step(robot, clock, 2.1)
    robot.flush_incident()
    contexts = [row for row in rows(robot)[1:] if row["tier"] == "context"]
    assert set(NECK_MOTORS) <= set(contexts[0]["axes"])
    latest = contexts[-1]
    assert latest["hardware_errors"] == {LEG_MOTORS[0]: 32}
    if neck_fails:
        assert latest["t_neck"] is None
        assert set(NECK_MOTORS).isdisjoint(latest["axes"])
    else:
        assert latest["t_neck"] == latest["t"]
        assert set(NECK_MOTORS) <= set(latest["axes"])


def test_robot_logs_leg_guards_off_and_records_neck_context(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="palmimo_sdk.robot")
    clock = Clock()
    driver = FlightDriver(clock, tmp_path / "incidents")
    robot = Palmimo(
        driver=driver,
        auto_wake=False,
        thermal_clock=clock,
        incident_dir=driver.directory,
        arm_overload_current=None,
        leg_overload_current=None,
        rail_undervoltage_v=None,
    )
    robot.connect()
    assert (
        len(
            [
                record
                for record in caplog.records
                if record.name == "palmimo_sdk.robot" and record.levelno == logging.INFO
            ]
        )
        == 1
    )
    driver.bits["neck_yaw"] = 4
    step(robot, clock, 1)
    step(robot, clock, 3.25)
    assert all(row["tier"] == "context" for row in rows(robot)[1:])
    assert set(rows(robot)[-1]["axes"]) == set(NECK_MOTORS)


def test_robot_driver_without_hardware_error_method_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class LegacyDriver(FlightDriver):
        def __getattribute__(self, name: str) -> Any:
            if name == "read_hardware_errors":
                raise AttributeError(name)
            return super().__getattribute__(name)

    clock = Clock()
    driver = LegacyDriver(clock, tmp_path / "incidents")
    robot = Palmimo(driver=driver, auto_wake=False, thermal_clock=clock)
    robot.connect()
    for now in range(5):
        step(robot, clock, now)
    assert len(driver.writes) == 5
    assert (
        len(
            [
                record
                for record in caplog.records
                if record.name == "palmimo_sdk.robot" and record.levelno == logging.WARNING
            ]
        )
        == 1
    )


@pytest.mark.parametrize("failure", [RuntimeError("serialization failed"), KeyboardInterrupt()])
def test_robot_dump_failure_happens_after_goal_write_and_keeps_interrupt_semantics(
    rig: tuple[Palmimo, FlightDriver, Clock],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: BaseException,
) -> None:
    robot, driver, clock = rig
    latch(robot, driver, clock)

    def fail(*args: Any, **kwargs: Any) -> str:
        raise failure

    monkeypatch.setattr(json, "dumps", fail)
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            step(robot, clock, 3.02)
    else:
        step(robot, clock, 3.02)
        assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert driver.events[-1][:2] == ("write", 3.02)
    assert robot.last_incident_path is None
    assert not list(driver.directory.iterdir())


def test_robot_connect_read_failure_defers_axis_baseline_until_first_answer(tmp_path: Path) -> None:
    clock = Clock()
    driver = FlightDriver(clock, tmp_path / "incidents")
    driver.read_failure = OSError("bus unavailable")
    robot = Palmimo(driver=driver, auto_wake=False, thermal_clock=clock, incident_dir=driver.directory)
    robot.connect()
    assert robot.is_connected
    assert len([event for event, _, _ in driver.events if event == "errors"]) == 3
    driver.read_failure = None
    driver.bits[LEG_MOTORS[0]] = 32
    step(robot, clock, 1)
    step(robot, clock, 1.01)
    assert robot.flush_incident() is None
    driver.bits[LEG_MOTORS[0]] = 0
    step(robot, clock, 2.1)
    step(robot, clock, 2.11)
    driver.bits[LEG_MOTORS[0]] = 32
    step(robot, clock, 3.2)
    step(robot, clock, 3.21)
    assert robot.flush_incident() is not None


@pytest.mark.parametrize("fps", [30, 60])
@pytest.mark.parametrize("latch_frame", [60, 61, 137, 240])
def test_robot_regular_steps_capture_full_post_window(tmp_path: Path, fps: int, latch_frame: int) -> None:
    clock = Clock()
    driver = FlightDriver(clock, tmp_path / "incidents")
    robot = Palmimo(driver=driver, fps=fps, auto_wake=False, thermal_clock=clock, incident_dir=driver.directory)
    robot.connect()
    for frame in range(latch_frame + fps * 4 + 1):
        if frame == latch_frame:
            driver.bits[LEG_MOTORS[0]] = 32
        step(robot, clock, frame / fps)
    assert rows(robot)[0]["truncated"] is False
    assert len(list(driver.directory.glob("*.jsonl"))) == 1


@pytest.mark.parametrize("failure", ["exception", "unreached"])
def test_robot_connect_retries_before_wake_and_records_wake_latch(tmp_path: Path, failure: str) -> None:
    class TransientDriver(FlightDriver):
        attempts = 0

        def read_hardware_errors(self, motors: Sequence[str] | None = None) -> HardwareErrorRead:
            self.attempts += 1
            if self.attempts == 1:
                if failure == "exception":
                    raise OSError("bus starting")
                return HardwareErrorRead(unreached=tuple(self.bits))
            return super().read_hardware_errors(motors)

    clock = Clock()
    driver = TransientDriver(clock, tmp_path / "incidents")
    driver.after_write = lambda: driver.bits.update({LEG_MOTORS[0]: 32})
    robot = Palmimo(driver=driver, thermal_clock=clock, incident_dir=driver.directory)
    robot.connect()
    assert driver.attempts == 2
    for frame in range(1, 241):
        step(robot, clock, frame / 60)
    assert rows(robot)[0]["trigger_bits"] == {LEG_MOTORS[0]: 32}


def test_robot_connect_stops_baseline_retries_on_partial_answer(tmp_path: Path) -> None:
    class PartialDriver(FlightDriver):
        attempts = 0

        def read_hardware_errors(self, motors: Sequence[str] | None = None) -> HardwareErrorRead:
            self.attempts += 1
            return HardwareErrorRead({LEG_MOTORS[0]: 0}, unreached=tuple(self.bits)[1:])

    clock = Clock()
    driver = PartialDriver(clock, tmp_path / "incidents")
    robot = Palmimo(driver=driver, auto_wake=False, thermal_clock=clock, incident_dir=driver.directory)
    robot.connect()
    assert driver.attempts == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_robot_out_of_range_goal_warns_once(tmp_path: Path, caplog: pytest.LogCaptureFixture, enabled: bool) -> None:
    clock = Clock()
    driver = FlightDriver(clock, tmp_path / "incidents")
    robot = Palmimo(driver=driver, auto_wake=False, thermal_clock=clock, flight_recorder=enabled)
    robot.connect()
    robot._write_goal(driver, {LEG_MOTORS[0]: 5000})
    assert driver.writes[-1] == {LEG_MOTORS[0]: 3900}
    assert len([record for record in caplog.records if record.levelno == logging.WARNING]) == 1


def test_robot_flushes_after_external_driver_disconnect(rig: tuple[Palmimo, FlightDriver, Clock]) -> None:
    robot, driver, clock = rig
    latch(robot, driver, clock)
    driver.connected = False
    writes = len(driver.writes)
    step(robot, clock, 3.01)
    assert robot.last_incident_path is None
    step(robot, clock, 3.26)
    assert rows(robot)[0]["truncated"] is True
    assert len(driver.writes) == writes
