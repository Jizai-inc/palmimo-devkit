from dataclasses import dataclass, field

from lerobot.cameras import CameraConfig
from lerobot.robots import RobotConfig


@RobotConfig.register_subclass("palmimo")
@dataclass
class PalmimoConfig(RobotConfig):
    """Configuration for Palmimo robot."""

    port: str = "/dev/ttyUSB0"
    baudrate: int = 1000000
    # Hardware is XC330-M288 (model 1240); xl330-m288 fails the connect handshake.
    motor_model: str = "xc330-m288"
    keep_torque_on_disconnect: bool = False
    cameras: dict[str, CameraConfig] = field(default_factory=dict)
