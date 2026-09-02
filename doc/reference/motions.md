# Motions and Gait Parameters

## Available Motions

Defined in the `Motion` enum in [engine.py](../../packages/palmimo_sdk/palmimo_sdk/engine.py).
The following 18 are callable through the Python API.

| Enum value | String name | Description |
|--------|--------|------|
| `IDLE` | `idle` / `stop` | Smoothly return to the neutral stance |
| `FORWARD` | `forward` | Move forward (tripod gait) |
| `BACKWARD` | `backward` | Move backward (tripod gait) |
| `STRAFE_LEFT` | `strafe_left` | Strafe left |
| `STRAFE_RIGHT` | `strafe_right` | Strafe right |
| `ROTATE_LEFT` | `rotate_left` | Turn in place, counter-clockwise (CCW) |
| `ROTATE_RIGHT` | `rotate_right` | Turn in place, clockwise (CW) |
| `CREEP` | `creep` | Low-speed creep gait, moving one leg at a time (most stable) |
| `DANCE` | `dance` | Sway the body — a dance |
| `BODY_TILT` | `body_tilt` / `tilt` | Tilt left/right (an "interested" pose) |
| `PUSHUP` | `pushup` / `push_up` | Raise and lower the body — a pushup |
| `WAVE` | `wave` / `wave_right` / `wave_left` | Greet with the front-right leg by default (`wave_right`); `wave_left` mirrors it on the front-left leg |
| `WAVE_BOTH` | `wave_both` | Greet with both front legs at once (first brings the middle legs forward and leans the body back with the nose raised into a "beg" pose, then waves both) |
| `CLAP` | `clap` | Stay in the beg pose and open/close both front legs (hands) on the yaw axis to clap (there's a lower bound so the feet don't touch) |
| `BOW` | `bow` | Raise the front legs and lower the chest and head in a bow (settle → hold → slowly rise, one-shot) |
| `STRETCH` | `stretch` | Keep the feet planted and fold every leg's femur to raise the body in a stretch (settle → rise → hold → lower, one-shot) |
| `NOD` | `nod` | Nod "yes" (neck only, one-shot) |
| `HEAD_SHAKE` | `head_shake` / `shake_head` | Shake the head "no" (neck only, one-shot) |

The key list for specifying a motion by string with `set_motion(name)` is defined in
`Palmimo._MOTION_MAP` in [robot.py](../../packages/palmimo_sdk/palmimo_sdk/robot.py).

For the methods that start each motion, see
[Core Methods](api-reference.md#core-methods) in the API reference.

## Gait Parameters

Defined in `MotionEngine.__init__` in
[engine.py](../../packages/palmimo_sdk/palmimo_sdk/engine.py).

| Parameter | Default | Unit | Description | Settable via |
|----------|------|-----|------|------|
| `gait_speed` | `0.012` | 0-1 | Phase increment per step (1 = one full cycle) | `MotionEngine` or `Palmimo` constructor |
| `step_length` | `30.0` | mm | Foot stride in the forward direction | `MotionEngine` or `Palmimo` constructor |
| `step_height` | `30.0` | mm | Foot lift height during swing (applies to walking and turning) | `MotionEngine` or `Palmimo` constructor |
| `yaw_amplitude` | `250` | tick | Max yaw-joint amplitude while walking (offset from center 2048) | `MotionEngine` constructor only — `Palmimo` does not forward it |

From a `Palmimo` instance, reach the knobs it does not forward through the engine
it owns: `robot.engine.yaw_amplitude = 200`.

Per-gesture tuning (wave, nod, head shake, dance) lives in the
[API reference](api-reference.md).

## Implementation Files

| Path | Role |
|-----|------|
| [packages/palmimo_sdk/palmimo_sdk/engine.py](../../packages/palmimo_sdk/palmimo_sdk/engine.py) | The walking engine itself (IK + gait generation, pure computation) |
| [packages/palmimo_sdk/palmimo_sdk/robot.py](../../packages/palmimo_sdk/palmimo_sdk/robot.py) | High-level Python API (`Palmimo` class, choreography playback) |
