# How Palmimo Moves

Walking and motion computation is consolidated in `palmimo_sdk`'s `MotionEngine`,
which is the single source of truth for gait direction and parameters. Every
front-end — the Python API and the example agent apps under `examples/` — is
driven through it.

| Path | Best for | How to call it | Implementation files |
|------|--------------|-----------|------------|
| **MotionEngine + Python API** | Driving walking from your own script / composing choreography | `from palmimo_sdk import Palmimo` | [engine.py](../../packages/palmimo_sdk/palmimo_sdk/engine.py), [robot.py](../../packages/palmimo_sdk/palmimo_sdk/robot.py) |

> **Note**: Front-ends built on top of the SDK delegate leg gait to the same
> `MotionEngine`. Do any gait parameter tuning or new-gait implementation work on
> the engine, not in a front-end.

## The Kinematic Chain

Values taken from the URDF (`MotionEngine` class variables `L1` / `L2` / `L3` in
[engine.py](../../packages/palmimo_sdk/palmimo_sdk/engine.py)).

| Symbol | Value (mm) | Part |
|-----|--------|------|
| `L1` | 32.0 | Coxa (mount to pitch1) |
| `L2` | 45.3 | Femur (pitch1 to pitch2) |
| `L3` | 100.0 | Tibia (pitch2 to foot tip) |

## Why the Gait Is a Tripod

`MotionEngine.TRIPOD_A` / `TRIPOD_B` in
[engine.py](../../packages/palmimo_sdk/palmimo_sdk/engine.py) split the six legs
into two groups.

| Group | Legs |
|---------|------|
| Tripod A | Leg 1 (RL) / Leg 3 (FL) / Leg 5 (MR) |
| Tripod B | Leg 2 (ML) / Leg 4 (RR) / Leg 6 (FR) |

A and B always move in anti-phase, so three legs stay on the ground at every
instant and the body never has to balance dynamically. For the mapping between
leg number and physical position (e.g. RL = rear-left), see
[motor_layout.drawio.svg](../images/motor_layout.drawio.svg).

Reading the comments and code in
[engine.py](../../packages/palmimo_sdk/palmimo_sdk/engine.py) gives the full
picture; the constants above are the ones worth knowing before you read it.

## Related Documents

- [Available motions and gait parameters](../reference/motions.md) — the motion list and the knobs
- [Controlling motions](../guides/controlling-motions.md) — driving the robot from Python
- [System architecture](architecture.md) — where the engine sits in the stack
