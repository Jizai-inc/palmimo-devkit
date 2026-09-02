# Motion Development Guide

Step-by-step guide for adding new motions to Palmimo.

## Prerequisites

- Read [explanation/architecture.md](../explanation/architecture.md) to understand the separation of concerns
- See [motor_layout.drawio.svg](../images/motor_layout.drawio.svg) for the servo layout and leg numbering
- See the README's [Safety](../../README.md#-safety) section for the servo range limits
- Complete the setup in [installation.md](installation.md)

## Step 1: Define the Motion

In `engine.py`, add to the Motion enum:

```python
class Motion(Enum):
    # ... existing motions ...
    MY_NEW_MOTION = auto()
```

Existing motions: IDLE, FORWARD, BACKWARD, STRAFE_LEFT, STRAFE_RIGHT, ROTATE_LEFT, ROTATE_RIGHT, DANCE, BODY_TILT, PUSHUP, WAVE, WAVE_BOTH, CLAP, CREEP, BOW, STRETCH, NOD, HEAD_SHAKE

## Step 2: Implement the Gait Method

```python
def _apply_my_new_motion(self) -> None:
    """Describe what this motion looks like visually."""
    self._gait_phase = (self._gait_phase + self.gait_speed) % 1.0
    phase_rad = self._gait_phase * 2.0 * math.pi

    # Option A: IK-based (preferred for walking/stepping)
    for leg_id in self.TRIPOD_A:
        dx = 10.0 * math.sin(phase_rad)           # mm forward/back
        dy = 0.0                                    # mm left/right
        dz = 5.0 * max(0, math.sin(phase_rad))    # mm up (lift)
        self._set_leg_from_ik(leg_id, dx, dy, dz)

    # Option B: Direct servo ticks (for simple oscillations)
    for leg_id in self.TRIPOD_B:
        offset = int(200 * math.sin(phase_rad))
        self._leg[f"leg_{leg_id}_pitch1"] = 2048 + offset
```

## Step 3: Wire the Dispatch

In the `step()` method:

```python
elif self._motion == Motion.MY_NEW_MOTION:
    self._apply_my_new_motion()
```

## Step 4: Add API Mapping

In `robot.py`:

```python
_MOTION_MAP = {
    # ... existing ...
    "my_new_motion": Motion.MY_NEW_MOTION,
}

def my_new_motion(self) -> None:
    """Start my new motion."""
    self._engine.motion = Motion.MY_NEW_MOTION
```

## Step 5: Expose to LLM Tool Calling (palmimo_sdk.agent)

A new facade motion is invisible to LLM agents until it has a tool. In
`palmimo_sdk/agent/tools.py`, add an `Expressive` subclass — not `Tool`
directly — so the motion picks up the optional `face` / `say` arguments every
other locomotion/gesture/neck tool has (talk and/or change expression while
moving); implement `_act()` (not `execute()`) following the `_run_motion`
pattern — set motion → `run(seconds=)` → guaranteed `stop()`. Set
`long_running: ClassVar[bool] = True` (a timed, blocking motion is one a
caller may race against a `Palmimo.cancel()`/`MotionCancelled` interruption —
see `robot.cancel()`); leave it at the `Tool` base's default `False` only for
a tool with no blocking `run()` underneath at all (`set_face`/`show_emoji`/
`say`/`capture`-style calls), or one that is deliberately excluded despite
calling `run()` (see `stop`'s own comment in `tools.py`). Then
register the tool in `TOOL_MODELS` in `palmimo_sdk/agent/toolset.py` (the
single source of truth for what ships with the SDK). Add the matching
schema/dispatch tests in `packages/palmimo_sdk/tests/test_agent_tools.py`.

`long_running=True` is not only for timed locomotion/gesture tools driven
through `_run_motion` — `look`/`look_center` are also `long_running=True`,
because they block on `robot.run(seconds=_NECK_SETTLE_SECONDS)` to actually
stream the neck target to the driver (setting the target alone doesn't move
anything). The only tools left at the `Tool` base's default `False` are ones
that are either instant with no blocking `run()` underneath (`set_face`,
`show_emoji`, `say`, `capture`), or — like `stop` — deliberately excluded
even though they do call `run()`: racing `stop()` against a cancellation is
pointless, since `stop()` is itself the tool an agent reaches for to halt an
in-flight action.

Skip this step only when the motion is deliberately not for LLM use — leave a
comment in `toolset.py` saying so, so the omission reads as a decision rather
than an oversight.

## Step 6: Test

### Automated Regression

Add or update tests under `packages/palmimo_sdk/tests/` before running hardware.
Use the SDK test command in [CONTRIBUTING.md](../../CONTRIBUTING.md#running-the-checks).

At minimum, exercise the full motion through completion and assert the safe
tick range, smooth return to neutral, stable tail, both left/right variants,
and boundary values for every public tuning knob. Deterministic acceptance
criteria belong in pytest, not in a one-off verification script.

### Dry Run

Drive the new motion through the Python API with no driver attached (see
[Try the Python API](../../README.md#try-the-python-api-no-hardware-needed)) and
inspect the `step()` output.

### Verify Safety
- All servo positions within 0-4095
- No extreme values (< 200 or > 3900)
- Smooth transition from neutral -> motion -> neutral
- No abrupt jumps between consecutive frames

### Live Test

After dry-run and suspended-hardware checks pass, drive the motion against
real hardware through a `Palmimo` instance with a connected `DynamixelDriver`
(see [Peripherals & Connection](../reference/api-reference.md#peripherals--connection)).
- Motion looks as intended visually
- No unusual servo sounds (grinding, clicking)
- Robot remains stable (doesn't tip over)
- `stop()` returns smoothly to neutral

Subjective or physical properties such as balance, noise, tracking lag, and
temperature cannot be decided by pytest — check those by running the motion on
real hardware with the robot lifted off the ground.

## Building Blocks Reference

| Tool | Usage | Notes |
|------|-------|-------|
| `self._set_leg_from_ik(leg_id, dx, dy, dz)` | Foot position in mm | Preferred for walking |
| `self._leg[f"leg_{id}_yaw"]` | Direct servo tick | Center = 2048 |
| `self._leg[f"leg_{id}_pitch1"]` | Upper leg joint tick | Center = 2048 |
| `self._leg[f"leg_{id}_pitch2"]` | Lower leg joint tick | Center = 2048 |
| `self._gait_phase` | 0.0-1.0 cycle | Use for periodic motions |
| `self.gait_speed` | Phase increment per step | Controls motion speed |
| `TRIPOD_A` / `TRIPOD_B` | [1,3,5] / [2,4,6] | Alternating gait groups |
| `LEFT_LEGS` / `RIGHT_LEGS` | [1,2,3] / [4,5,6] | Side groups |
| `self.NEUTRAL` | 2048 | Center position constant |

## Choreography

Once tested, add your motion to choreography sequences:

```python
routine = [
    ("forward", 2.0),
    ("my_new_motion", 3.0),
    ("idle", 1.0),
]
for pos in robot.play(routine, fps=60):
    send_to_hardware(pos)
```
