import contextlib
from typing import Any

from lerobot.cameras import make_cameras_from_configs
from lerobot.robots import Robot

from palmimo_sdk import DynamixelDriver, ServoDriver, palmimo_motor_ids
from palmimo_sdk import Palmimo as PalmimoFacade

from .config_palmimo import PalmimoConfig


# The normalized action range this plugin accepts, mapped onto raw Dynamixel
# ticks: -1.0 -> 1024, 0.0 -> 2048 (center), 1.0 -> 3072. Narrower than the
# driver's own safe-range clamp, which stays the last gate on every write.
_NORMALIZED_CENTER_TICK = 2048
_NORMALIZED_HALF_SPAN_TICKS = 1024


class Palmimo(Robot):
    """Palmimo robot implementation for LeRobot.

    A thin adapter, not a second hardware stack: every servo read and write goes
    through ``palmimo_sdk``. The facade owns the connection lifecycle — the
    gain-ramped rise out of limp on connect, and the park plus neck soft-release
    on disconnect — so a LeRobot session starts and stops the robot exactly the
    way the SDK does. ``lerobot.robots.Robot`` requires ten members and a motor
    bus is not among them; the cameras are the only hardware this class opens
    itself, because a LeRobot observation carries LeRobot camera frames.
    """

    config_class = PalmimoConfig
    name = "palmimo"

    def __init__(self, config: PalmimoConfig, *, driver: ServoDriver | None = None):
        """Build the plugin around an SDK servo driver.

        Args:
            config: The LeRobot robot configuration.
            driver: Servo backend to drive. ``None`` (the normal path, and what
                LeRobot's own construction gives) builds the SDK's
                ``DynamixelDriver`` from *config*. Passing one lets an embedder
                that already owns a driver share it — and lets tests exercise
                this class against a backend that is not a serial port.
        """
        super().__init__(config)
        self.config = config

        self._driver = driver or DynamixelDriver(
            port=config.port,
            baudrate=config.baudrate,
            motor_model=config.motor_model,
            keep_torque_on_disconnect=config.keep_torque_on_disconnect,
        )
        # auto_wake is deferred rather than declined: connect() runs the wake
        # glide itself, after the cameras are open, so the robot does not move
        # while a camera is still being brought up (and so connect(reset_pose=False)
        # can skip the motion entirely).
        self._robot = PalmimoFacade(driver=self._driver, auto_wake=False)
        self.cameras = make_cameras_from_configs(config.cameras)

    @property
    def _motors_ft(self) -> dict[str, type]:
        return {f"{motor}.pos": float for motor in palmimo_motor_ids()}

    @property
    def _cameras_ft(self) -> dict[str, tuple]:
        return {cam: (self.cameras[cam].height, self.cameras[cam].width, 3) for cam in self.cameras}

    @property
    def observation_features(self) -> dict:
        return {**self._motors_ft, **self._cameras_ft}

    @property
    def action_features(self) -> dict:
        return self._motors_ft

    @property
    def is_connected(self) -> bool:
        return self._robot.is_connected and all(cam.is_connected for cam in self.cameras.values())

    def connect(self, calibrate: bool = True, configure: bool = True, reset_pose: bool = True) -> None:
        """Connect to the robot and rise out of limp into the neutral stance.

        Args:
            calibrate: Accepted for the LeRobot interface; Palmimo is calibrated
                during assembly, so this never runs a calibration.
            configure: Accepted for the LeRobot interface; the SDK driver sets
                the operating mode as part of its own connect.
            reset_pose: Whether to run the SDK's wake glide into neutral. ``False``
                opens the bus and leaves the robot where it stands.
        """
        self._robot.connect()
        try:
            for cam in self.cameras.values():
                cam.connect()
            if reset_pose:
                # Limp -> neutral with Position_P_Gain ramping up underneath, so
                # the robot firms up instead of snapping into the stance.
                self._robot.wake()
        except BaseException:
            # A camera failure (or a Ctrl+C during the ~1.5s glide) must not leave
            # the robot armed with nobody holding it. Best-effort so the rollback
            # never replaces the error that caused it.
            with contextlib.suppress(Exception):
                self.disconnect()
            raise

    def disconnect(self) -> None:
        """Park the robot and release the bus, then close the cameras.

        The default shutdown is the SDK's: ease the legs back to neutral,
        soft-release the neck by ramping its gain down, then cut torque.
        ``keep_torque_on_disconnect`` closes through the driver instead, because
        holding a pose and soft-releasing the neck are contradictory — the ramp
        exists to make a torque-off safe, and with torque left on it would only
        let the head sag.
        """
        if self.config.keep_torque_on_disconnect:
            self._driver.disconnect()
        else:
            self._robot.disconnect(park=True)
        for cam in self.cameras.values():
            with contextlib.suppress(Exception):
                cam.disconnect()

    @property
    def is_calibrated(self) -> bool:
        # Palmimo is pre-calibrated during assembly
        return True

    def calibrate(self) -> None:
        """Not reachable: :attr:`is_calibrated` is always ``True``.

        Raises:
            NotImplementedError: Always. Joint zeros are set in the servos'
                EEPROM during assembly, and the SDK exchanges raw ticks against
                those zeros, so there is no per-session calibration to record.
        """
        raise NotImplementedError(
            "Palmimo is calibrated during assembly and reports is_calibrated=True; "
            "there is no per-session calibration to run."
        )

    def configure(self) -> None:
        """No-op: the SDK driver configures the servos when it connects.

        It writes the position operating mode, seeds each goal with the pose the
        servo is already holding, and applies the default position gain before
        arming torque — all inside :meth:`connect`, where it belongs.
        """

    def get_observation(self) -> dict[str, Any]:
        if not self.is_connected:
            raise ConnectionError(f"{self} is not connected.")

        positions = self._driver.read_positions()
        # The driver answers a dropped batch read with an empty (or short) dict
        # rather than raising. Left alone that becomes a frame missing declared
        # features, which surfaces far downstream as a dataset shape error.
        missing = [motor for motor in palmimo_motor_ids() if motor not in positions]
        if missing:
            raise ConnectionError(
                f"{self} read no position for {len(missing)} servo(s): {', '.join(missing)}. "
                "Check the servo bus connection and power."
            )
        obs_dict: dict[str, Any] = {f"{motor}.pos": positions[motor] for motor in palmimo_motor_ids()}

        for cam_key, cam in self.cameras.items():
            obs_dict[cam_key] = cam.async_read()

        return obs_dict

    def send_action(self, action: dict[str, Any], normalize: bool = False) -> dict[str, Any]:
        """Send motor positions to the robot.

        Args:
            action: Motor positions, for example ``{"leg_1_yaw.pos": 2048}``. Every
                joint in :attr:`action_features` must be present.
            normalize: Whether values are normalized to the range -1.0 to 1.0.

        Returns:
            The action that was sent.

        Raises:
            ValueError: If the action does not name every joint. The driver fills
                an absent joint with neutral, so a partial action would silently
                command the joints it left out — a leg sweep asked for by nobody.
        """
        goal_pos: dict[str, int] = {}
        for key, val in action.items():
            motor_name = key.removesuffix(".pos")
            if normalize:
                raw_val = int(_NORMALIZED_CENTER_TICK + val * _NORMALIZED_HALF_SPAN_TICKS)
                goal_pos[motor_name] = max(
                    _NORMALIZED_CENTER_TICK - _NORMALIZED_HALF_SPAN_TICKS,
                    min(_NORMALIZED_CENTER_TICK + _NORMALIZED_HALF_SPAN_TICKS, raw_val),
                )
            else:
                goal_pos[motor_name] = int(val)

        missing = [motor for motor in palmimo_motor_ids() if motor not in goal_pos]
        if missing:
            raise ValueError(
                f"send_action needs every joint in action_features; missing {len(missing)}: {', '.join(missing)}."
            )

        self._driver.write_positions(goal_pos)
        return action
