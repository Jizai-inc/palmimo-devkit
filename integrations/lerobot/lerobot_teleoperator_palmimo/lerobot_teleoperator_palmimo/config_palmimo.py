from dataclasses import dataclass, field

from lerobot.teleoperators.config import TeleoperatorConfig


@TeleoperatorConfig.register_subclass("palmimo")
@dataclass
class PalmimoTeleopConfig(TeleoperatorConfig):
    """Configuration for Palmimo WASD keyboard teleoperator."""

    # Character-key map (modelled on lerobot's lekiwi teleop_keys). Input comes
    # from lerobot's native KeyboardTeleop, which only reports key.char keys —
    # arrow keys are not available, so neck control uses character keys too.
    teleop_keys: dict[str, str] = field(
        default_factory=lambda: {
            # Locomotion
            "forward": "w",
            "backward": "s",
            "strafe_left": "a",
            "strafe_right": "d",
            "rotate_left": "q",
            "rotate_right": "z",
            "dance": "e",
            # Neck (remapped from arrow keys to free character keys)
            "neck_pitch_up": "o",
            "neck_pitch_down": "l",
            "neck_yaw_left": "n",
            "neck_yaw_right": "m",
        }
    )
