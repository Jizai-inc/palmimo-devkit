# Palmimo Hexapod Teleoperator

A WASD keyboard teleoperator for the Palmimo hexapod robot. Keyboard input is
read through lerobot's native `KeyboardTeleop` (pynput-based, no display window).
Leg motion is delegated to `palmimo_sdk`'s `MotionEngine` — the hardware-validated
source of truth for gait and IK — while this teleop maps held keys to a motion
and to independent neck control.

## Features

- **WASD + QZE Controls**: Gaming-style controls for locomotion (forward/back,
  strafe, rotate, dance)
- **Tripod Gait via MotionEngine**: Alternating tripod gait computed by the SDK
- **Independent Neck Control**: Look around while walking (character keys)
- **IK Calibration Keys**: Per-leg and whole-body IK / debug poses (`1`-`0`,
  `R/F/V/B`, `T/Y/U/I/G/H/J/K`). These write a leg pose directly, bypassing
  `MotionEngine`'s smoothing — `R/F/V/B` and `T/Y/U/I/G/H/J/K` step legs
  ±200 ticks from neutral in one frame; use them with the robot in the air.

## Installation

The teleoperator is a member of the `integrations/lerobot/` uv workspace, which
is separate from the repository's top-level uv workspace. It declares `lerobot`
and `palmimo-sdk` in its `pyproject.toml`; `pynput` is not declared here and
arrives transitively through lerobot. Installation and launch commands live in the
[LeRobot workspace README](../README.md#setup).

## Controls

Keys are character keys (the native `KeyboardTeleop` reports `key.char`, so arrow
keys are not used). All mappings live in `config.teleop_keys` and can be
overridden.

### Body Movement

| Key | Action | Description |
|-----|--------|-------------|
| `W` | Forward | Tripod gait — legs (1,3,5) and (2,4,6) alternate |
| `S` | Backward | Tripod gait reversed |
| `A` | Strafe Left | Left side legs (1,2,3) push |
| `D` | Strafe Right | Right side legs (4,5,6) push |
| `Q` | Rotate Left | Counter-clockwise rotation (CCW) |
| `Z` | Rotate Right | Clockwise rotation (CW) |
| `E` | Dance | Body sway |

### Neck Movement

| Key | Action | Description |
|-----|--------|-------------|
| `O` | Look Up | Increase neck pitch |
| `L` | Look Down | Decrease neck pitch |
| `N` | Look Left | Increase neck yaw |
| `M` | Look Right | Decrease neck yaw |

### System

| Key | Action |
|-----|--------|
| `ESC` | Quit |

> **Note**: Body and neck controls work independently — you can walk and look
> around simultaneously.

## Leg Layout

The hexapod has 6 legs arranged symmetrically, viewed from above with the neck
(front) pointing up. The repository-wide motor layout diagram is the visual
source of truth:

![Motor Layout](../../../doc/images/motor_layout.drawio.svg)

### Leg Naming Convention

| Leg | Position | Side | Motor IDs |
|-----|----------|------|-----------|
| 1 | Rear Left (RL) | Left | yaw: 1, pitch1: 2, pitch2: 3 |
| 2 | Middle Left (ML) | Left | yaw: 4, pitch1: 5, pitch2: 6 |
| 3 | Front Left (FL) | Left | yaw: 7, pitch1: 8, pitch2: 9 |
| 4 | Rear Right (RR) | Right | yaw: 10, pitch1: 11, pitch2: 12 |
| 5 | Middle Right (MR) | Right | yaw: 13, pitch1: 14, pitch2: 15 |
| 6 | Front Right (FR) | Right | yaw: 16, pitch1: 17, pitch2: 18 |

### Neck Motors

| Joint | Motor ID |
|-------|----------|
| pitch1 | 19 |
| pitch2 | 20 |
| yaw | 21 |

## Usage with LeRobot

The teleop is registered as `palmimo` and is normally launched through
lerobot's CLI. Use the command in the
[LeRobot workspace README](../README.md#usage) so installation and CLI flags
stay synchronized with the workspace.

### Programmatic Usage

```python
from lerobot_teleoperator_palmimo import PalmimoTeleop, PalmimoTeleopConfig

config = PalmimoTeleopConfig()
teleop = PalmimoTeleop(config)

# Connect (starts the keyboard listener)
teleop.connect()

try:
    while teleop.is_connected:
        action = teleop.get_action()
        robot.send_action(action)
except KeyboardInterrupt:
    pass
finally:
    teleop.disconnect()
```

### Action Dictionary

`get_action()` returns positions for the registered motors:

```python
{
    "leg_1_yaw.pos": 2048,
    "leg_1_pitch1.pos": 2048,
    "leg_1_pitch2.pos": 2048,
    # ... legs 2-6 ...
    "neck_pitch1.pos": 2048,
    "neck_pitch2.pos": 2048,
    "neck_yaw.pos": 2048,
}
```

Position values are raw Dynamixel units (0-4095, center = 2048).

## Gait Patterns

Gait is computed by `palmimo_sdk.MotionEngine`. Summary of behaviour:

### Tripod Gait (Forward/Backward)

- **Tripod A**: Legs 1 (RL), 3 (FL), 5 (MR)
- **Tripod B**: Legs 2 (ML), 4 (RR), 6 (FR)

These groups move in opposite phases, keeping 3 legs in contact at all times.

### Strafe Motion

Lateral movement drives one side of legs.

### Rotation

Opposite sides move in opposite directions to turn in place.

## Configuration Options

| Parameter | Default | Description |
|-----------|---------|-------------|
| `teleop_keys` | see config | Character-key map for movement and neck |

## Troubleshooting

### No Keys Detected

`KeyboardTeleop` uses pynput. On macOS, grant the terminal **Accessibility**
permission. On Linux, a display (`DISPLAY`) is required — pynput is skipped in a
headless session, and `connect()` raises `DeviceNotConnectedError` before any
action is read. A desktop session is not required: a virtual framebuffer is
enough, and keys must be delivered to that display. See
[Display (Linux)](../README.md#display-linux) in the workspace README for the
`xvfb` setup.

### Motors Not Moving

1. Check that the robot is connected and motors are enabled.
2. Verify motor IDs match the expected configuration.
3. Ensure the action dictionary keys match your robot's motor names.

## License

Apache-2.0, the same as the rest of this workspace — see
[LICENSE](../LICENSE). Third-party components reached through LeRobot, including
the LGPLv3 `pynput` behind `KeyboardTeleop`, are recorded in
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Acknowledgments

- Built for the Palmimo hexapod robot
- Integrates with the LeRobot teleoperation framework
