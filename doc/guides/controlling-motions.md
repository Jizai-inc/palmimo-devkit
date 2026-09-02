# Controlling Motions from Python

To build walking into your own script, use the `Palmimo` class in
[robot.py](../../packages/palmimo_sdk/palmimo_sdk/robot.py). Every method it
exposes is listed in [Core Methods](../reference/api-reference.md#core-methods);
this page shows the shapes those calls take.

## Before You Run

| Step | Reference |
|-----|-----|
| Confirm all motors respond | [`diagnose_servos.py scan`](../../scripts/README.md) |
| Reset to neutral stance | `Palmimo.stop()` eases to neutral |
| Torque off (move by hand) | Exit the app / cut power (see [Safety](../../README.md#-safety)) |

Lift the unit so the feet are in the air the first time you run a new motion.

## Minimal Example (Dry Run)

```python
from palmimo_sdk import Palmimo

robot = Palmimo(gait_speed=0.012, step_length=30.0, step_height=30.0)
robot.forward()

for _ in range(120):              # ~2 seconds @ 60 Hz
    positions = robot.step()      # dict[str, int]: motor name -> tick (0-4095)
    print(positions)

robot.stop()
for _ in range(60):               # return to neutral
    robot.step()
```

> ⚠️ Ticks run 0-4095, but keep motion inside the safe range **200-3900** —
> the raw ends are mechanical limits. See
> [Safety Design](../explanation/architecture.md#safety-design).

## Sending to Real Hardware Too

The return value of `step()` (a dict of target ticks for the 18 leg motors plus
`neck_yaw` and `neck_pitch1`, 20 motors total — the neck's `neck_pitch2`, servo ID 20,
physically exists but `MotionEngine` holds no state for it and omits it from the
output) is what `DynamixelDriver.write_positions()` takes, so passing it there drives
real hardware. For the concrete bus-initialization sequence, see
[DynamixelDriver](../../packages/palmimo_sdk/palmimo_sdk/io/dynamixel.py) and the
[Peripherals & Connection](../reference/api-reference.md#peripherals--connection)
section of the API reference.

## Choreography

`Palmimo.play()` in [robot.py](../../packages/palmimo_sdk/palmimo_sdk/robot.py)
plays back a sequence of `(motion name, seconds)` steps. The special motion
`look_around` sweeps the neck in a sine wave. Note that with the `(name, seconds)`
tuple form, `motion` stays at the default `Motion.IDLE`, so the legs stay still during
that segment. To sweep the neck while walking, pass
`RoutineStep(motion=..., neck_sweep=True)` instead of a tuple.

```python
routine = [
    ("forward",     2.0),
    ("rotate_left", 1.0),
    ("dance",       3.0),
    ("look_around", 2.0),
    ("idle",        0.5),
]

robot = Palmimo()
for positions in robot.play(routine, fps=60):
    # send positions to real hardware / a simulator
    ...
```

For real-time playback (paced internally with `time.sleep`), use
[`play_realtime`](../../packages/palmimo_sdk/palmimo_sdk/robot.py).

## Troubleshooting

- **Serial port not found**: Check the actual device name with `ls /dev/tty.usbmodem*`
  (macOS) or `ls /dev/ttyACM*` (Linux), then specify the port explicitly when
  constructing `DynamixelDriver`
- **Motors don't move / rattle**: Check power supply capacity and cable connections,
  and confirm individual IDs respond with `diagnose_servos.py scan`
- **Motion is erratic**: Always lift the unit so the feet are in the air, run against
  real hardware to check behavior, then set it down

## Related Documents

- [Motions and gait parameters](../reference/motions.md) — what you can call and what you can tune
- [How Palmimo moves](../explanation/motion-system.md) — why the gait is built this way
- [Motion development guide](motion-development-guide.md) — adding a new motion
