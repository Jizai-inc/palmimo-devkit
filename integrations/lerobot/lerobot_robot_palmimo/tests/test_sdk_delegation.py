"""The plugin drives servos through palmimo_sdk, and only through it.

The kit had two hardware paths — the SDK's and this plugin's own bus — and the
two startup/shutdown behaviors drifted apart, because nothing but prose said
they had to agree. These tests pin the delegation itself: every servo read and
write lands on the injected ``ServoDriver``, and connect/disconnect run the
SDK's wake glide and park rather than a copy of them.

No hardware and no bus: a stand-in driver records what it was asked to do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest


if TYPE_CHECKING:
    from lerobot_robot_palmimo.palmimo import Palmimo

pytest.importorskip("lerobot")

from palmimo_sdk import ServoDriver, palmimo_motor_ids


NEUTRAL = 2048


class _StandInDriver(ServoDriver):
    """A ServoDriver that answers from memory and records every command."""

    def __init__(self, positions: dict[str, int] | None = None):
        self._positions = positions or dict.fromkeys(palmimo_motor_ids(), NEUTRAL)
        # Stands in for a dropped batch read, which the SDK driver reports as a
        # short (or empty) dict rather than an exception.
        self.short_read: dict[str, int] | None = None
        self._connected = False
        self.goals: list[dict[str, int]] = []
        self.gains: list[tuple[int | None, tuple[str, ...] | None]] = []
        self.connects = 0
        self.disconnects = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True
        self.connects += 1

    def disconnect(self) -> None:
        self._connected = False
        self.disconnects += 1

    def write_positions(self, positions: dict[str, int]) -> None:
        self.goals.append(dict(positions))
        self._positions.update(positions)

    def read_positions(self) -> dict[str, int]:
        if self.short_read is not None:
            return dict(self.short_read)
        return dict(self._positions)

    def set_position_p_gain(self, value: int | None, motors: list[str] | None = None) -> None:
        self.gains.append((value, tuple(motors) if motors else None))

    def set_profile_velocity(self, ticks_per_second: int | dict[str, int]) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the SDK's timed ramps at full speed.

    The wake glide and the neck release are paced by ``time.sleep`` — ~4s of
    wall clock between them, all of it spent waiting on hardware that isn't here.
    """
    import palmimo_sdk.robot

    monkeypatch.setattr(palmimo_sdk.robot.time, "sleep", lambda _seconds: None)


def _robot(driver: ServoDriver, **config_kwargs: Any) -> Palmimo:
    from lerobot_robot_palmimo.config_palmimo import PalmimoConfig
    from lerobot_robot_palmimo.palmimo import Palmimo

    return Palmimo(PalmimoConfig(port="/dev/null", **config_kwargs), driver=driver)


def _full_action(tick: float = NEUTRAL) -> dict[str, float]:
    return {f"{motor}.pos": tick for motor in palmimo_motor_ids()}


def test_the_plugin_holds_no_motor_bus() -> None:
    """The bus attribute is the thing that made a second hardware path possible."""
    robot = _robot(_StandInDriver())

    assert not hasattr(robot, "bus")


def test_features_cover_every_servo_without_a_bus_to_ask() -> None:
    robot = _robot(_StandInDriver())

    assert set(robot.action_features) == {f"{motor}.pos" for motor in palmimo_motor_ids()}
    assert set(robot.observation_features) == set(robot.action_features)


def test_connect_opens_the_driver_and_rises_through_the_sdk_wake() -> None:
    """The rise out of limp is the SDK's gain ramp, not a bare goal write."""
    driver = _StandInDriver()
    robot = _robot(driver)

    robot.connect()

    assert driver.connects == 1
    assert robot.is_connected
    # wake() ramps Position_P_Gain up under the glide; a plain move never touches gain.
    leg_ramp = [value for value, motors in driver.gains if motors and "leg_1_yaw" in motors and value is not None]
    assert leg_ramp, "connect() did not run the SDK's gain-ramped wake"
    assert leg_ramp[0] < leg_ramp[-1]
    assert driver.goals, "connect() commanded no pose"
    assert driver.goals[-1]["leg_1_yaw"] == NEUTRAL


def test_connect_without_reset_pose_leaves_the_robot_where_it_stands() -> None:
    driver = _StandInDriver()
    robot = _robot(driver)

    robot.connect(reset_pose=False)

    assert driver.connects == 1
    assert driver.goals == []


def test_connect_rolls_back_when_a_camera_fails_to_open() -> None:
    """A half-open connect would leave the robot armed with nobody holding it."""

    class _FailingCamera:
        is_connected = False
        height = 480
        width = 640

        def connect(self) -> None:
            raise RuntimeError("camera busy")

        def disconnect(self) -> None:
            pass

    driver = _StandInDriver()
    robot = _robot(driver)
    robot.cameras = {"head": _FailingCamera()}

    with pytest.raises(RuntimeError, match="camera busy"):
        robot.connect()

    assert driver.disconnects == 1
    assert not driver.is_connected


def test_disconnect_parks_through_the_sdk_and_releases_the_neck() -> None:
    """Torque-off is preceded by the neck gain ramp-down, or the head drops."""
    driver = _StandInDriver()
    robot = _robot(driver)
    robot.connect()
    driver.gains.clear()

    robot.disconnect()

    neck_ramp = [value for value, motors in driver.gains if motors and "neck_pitch1" in motors and value is not None]
    assert neck_ramp, "disconnect() did not soft-release the neck"
    assert neck_ramp[0] > neck_ramp[-1]
    assert driver.disconnects == 1


def test_keeping_torque_skips_the_park_that_would_let_the_head_sag() -> None:
    """Holding a pose and softening the neck are contradictory requests."""
    driver = _StandInDriver()
    robot = _robot(driver, keep_torque_on_disconnect=True)
    robot.connect()
    driver.gains.clear()

    robot.disconnect()

    assert driver.gains == []
    assert driver.disconnects == 1


def test_get_observation_reads_positions_from_the_driver() -> None:
    positions = {motor: NEUTRAL + i for i, motor in enumerate(palmimo_motor_ids())}
    driver = _StandInDriver(dict(positions))
    robot = _robot(driver)
    robot.connect(reset_pose=False)

    observation = robot.get_observation()

    assert observation == {f"{motor}.pos": tick for motor, tick in positions.items()}


def test_get_observation_refuses_a_frame_that_is_missing_servos() -> None:
    """A dropped batch read answers short; a short frame breaks a dataset far away."""
    driver = _StandInDriver()
    robot = _robot(driver)
    robot.connect(reset_pose=False)
    driver.short_read = {"neck_yaw": NEUTRAL}

    with pytest.raises(ConnectionError, match="read no position"):
        robot.get_observation()


def test_send_action_writes_raw_ticks_through_the_driver() -> None:
    driver = _StandInDriver()
    robot = _robot(driver)
    robot.connect(reset_pose=False)

    robot.send_action({**_full_action(), "leg_1_yaw.pos": 2100})

    assert driver.goals[-1]["leg_1_yaw"] == 2100
    assert driver.goals[-1]["neck_yaw"] == NEUTRAL


def test_send_action_maps_the_normalized_range_onto_ticks() -> None:
    driver = _StandInDriver()
    robot = _robot(driver)
    robot.connect(reset_pose=False)

    robot.send_action({**_full_action(0.0), "leg_1_yaw.pos": 1.0, "leg_2_yaw.pos": -2.0}, normalize=True)

    assert driver.goals[-1]["leg_1_yaw"] == 3072
    assert driver.goals[-1]["leg_2_yaw"] == 1024  # clamped, not extrapolated
    assert driver.goals[-1]["neck_yaw"] == NEUTRAL


def test_a_partial_action_is_rejected_rather_than_backfilled() -> None:
    """The driver fills an absent joint with neutral: a partial action would
    command a leg sweep nobody asked for."""
    driver = _StandInDriver()
    robot = _robot(driver)
    robot.connect(reset_pose=False)

    with pytest.raises(ValueError, match="every joint"):
        robot.send_action({"neck_yaw.pos": 2100})

    assert driver.goals == []


def test_calibrate_says_so_instead_of_reaching_for_a_bus() -> None:
    robot = _robot(_StandInDriver())

    assert robot.is_calibrated
    with pytest.raises(NotImplementedError, match="calibrated during assembly"):
        robot.calibrate()
