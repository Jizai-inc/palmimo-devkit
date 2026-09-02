# User Diagnostics

Setup and DYNAMIXEL diagnostic tools for Palmimo DevKit users.
The tools here are supported and distributed to end users.

## Audience and Safety

All servo diagnostics live in one tool, `diagnose_servos.py`, one subcommand
per job.

| Subcommand | Audience | Hardware effect |
|------------|----------|-----------------|
| `scan` | Users verifying connections after unboxing or transport | Read-only |
| `power` | Users diagnosing servo power/wiring | Read-only |
| `errors` | Advanced users inspecting latched hardware errors | Read-only |
| `joints` | Users checking joint orientation, range, and position gain after a servo is re-seated | Read-only |
| `recover` | Advanced users recovering from errors | Reboots servos with latched errors |
| `oscillate` | Advanced users verifying a single servo after service | Drives the specified servo ±15° around its current position, ramped |

Before running any tool that drives real hardware, make sure the robot is
lifted off the ground and you're able to e-stop it. Stop any other app or
holder using the same serial port first.

`oscillate` is the only subcommand that moves the robot, and it moves it gently
on purpose: it swings around the joint's present position and writes a motion
profile first, so the servo ramps into each target instead of stepping to it.
An un-ramped position command makes a stiff servo (P gain 1100, no damping)
slam into the target at up to its `Velocity_Limit`, and that shock is what
damages gears, so the profile write is a safety measure — do not remove it to
make the check faster. If it could not read `Profile_Acceleration`/
`Profile_Velocity` before writing its own, it says so and runs anyway; that
run's profile then stays on the servo until a power cycle, so power-cycle the
robot before running the SDK when you see that warning. **It also leaves that
joint with torque off when it finishes**, and releases torque up front if the
servo has to be switched to position control (an EEPROM write the servo
rejects while torque is on) — so a robot that was holding a pose will drop
that joint. It prints a warning before either release, but it does not pause
for you: support the robot before you start the command. It also raises that
servo's `Position_P_Gain` to the SDK default for the duration of the swing and
restores it afterwards, because a servo left on a low gain accepts every goal
and drives none of them; it prints the gain it found and the gain it used, so
the change is never silent. The robot has no thermal protection: stop and let
the servos cool if one gets hot while you repeat the check. When you only need
to know where the joints are, which way they face, or what gain they are on,
use `joints` — it never writes.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) package manager
- The servo interface board, which ships pre-flashed as a USB–DYNAMIXEL bridge
    - Vendor reference: [the OpenRB-150 e-Manual](https://emanual.robotis.com/docs/en/parts/controller/openrb-150/)

## Scripts

### diagnose_servos.py

One CLI for the whole servo bus, talking to `dynamixel_sdk` directly. Each
subcommand accepts `--port`/`-p` (auto-detected when omitted) and
`--baudrate`/`-b` (default `1000000`).

- `scan` — pings every ID in a range (`--start`/`-s`, `--end`/`-e`; default
  `0`-`252`) and lists the responders with their model number. Use it to
  verify wiring and ID assignment after unboxing or transport.
- `power` — reads every servo's input voltage and current without ever
  enabling torque, so it is safe to run any time the bus is free. Prints a
  single snapshot by default; `--watch` keeps reading (`--interval`, default
  `0.4` s) so you can watch the voltage while adjusting the supply or
  re-seating wiring, and `--per-id` prints each servo's voltage. Press
  `Ctrl+C` to stop a `--watch` loop. Voltage is `Present_Input_Voltage`
  (0.1 V units, reliable); current is `Present_Current` (best-effort, treat as
  approximate).
- `errors` — pings all 21 servos and reports each one's hardware-error status,
  torque state, and voltage. Read-only.
- `joints` — pings all 21 servos and reports each one's present position (and
  its offset from the servo center in degrees), `Drive_Mode` (whether the joint
  is reversed, and whether its profile is time- or velocity-based), torque
  state, temperature, position limits, and `Position_P_Gain` (as `pgain`).
  Read-only: it never enables torque and never writes a goal, so it is the way
  to check assembly orientation and range without moving anything. `pgain` is
  what to look at when a joint did not move: a servo whose gain reads far below
  its peers' accepts every goal and drives none of them. The default is 900, and
  the SDK's shutdown park leaves the three neck servos (ids 19-21) on 6 until it
  next connects, so a neck reading 6 next to legs reading 900 is expected on a
  robot that has been run.
- `recover` — reboots (Dynamixel instruction 0x08) the servos whose hardware
  error has latched, then re-checks them. This is the "responds to ping but
  won't hold torque" case. Reboot only restarts firmware on a servo that is
  already powered and responding — it cannot restore power, so if no servos
  respond, the bus is electrically dead and the fix is the supply/wiring, not
  this tool.
- `oscillate` — swings one servo (`--id`, required) alternately ±15 degrees
  (~±170 units) around **its present position** for `--duration` seconds
  (default `5.0`, must be greater than 0), then takes one more swing to return
  the joint to where it started. Each swing takes about 1.25 s, so the default
  is 4 swings and about 6.3 s per servo. Centering on the present position keeps
  the first commanded step to one amplitude from any pose, instead of hauling
  the joint to a fixed center it may be 70° away from. It ensures position
  control mode (Operating Mode = 3, written only when it differs since that
  register is EEPROM), writes `Profile_Acceleration`/`Profile_Velocity` before
  any goal so every move is ramped and slow enough to watch (~0.9 s per move
  under the time-based `Drive_Mode`, ~1.4 s under the velocity-based one, plus
  ~0.35 s of settling before the joint is read), seeds the goal with the present position so
  enabling torque holds the pose rather than snapping, and clamps both targets to
  the servo's own `Min`/`Max_Position_Limit` narrowed to the safe tick range. It
  reads everything it needs before it writes anything, and refuses — leaving the
  servo untouched — when the limit window is narrower than 60 units (~5°), when
  those limits do not overlap the safe tick range, when the joint sits more than
  ~110 units outside them, or when `Torque_Enable`, `Operating_Mode`,
  `Drive_Mode`, the limits, or the present position do not come back off the bus
  after a few retries. It also stops, before writing anything, when the servo
  reports a latched hardware error (bit 7 of the status packet, which a latched
  servo sets on every packet it sends while still answering and still obeying):
  that is an `errors`/`recover` job, not a failed swing. On a normal exit it
  returns to the starting position, restores the profile registers it found, and
  disables torque, and it exits non-zero if any of that did not happen.

  It reads, sets, and restores `Position_P_Gain` alongside the profile
  registers, driving the swing at the SDK's default of 900 and printing both the
  gain it found and the gain it used. Without that it inherits whatever the last
  session left behind, and the SDK's shutdown park leaves the neck servos on a
  gain of 6 — low enough that the servo accepts every goal and moves the joint
  not at all. The gain goes in after the goal is seeded with the present
  position and before torque is enabled, which is the only order in which a
  raised gain cannot act on a stale goal and snap a drooping head to it.

  Finally, it checks that the joint actually followed. A commanded swing the
  joint ignores is invisible from the bus — every write is accepted and every
  reading is honest — so each reading is judged against the target it belongs
  to, and the worst of those errors has to stay within half of the largest
  excursion the run commanded (85 units on a full ±170 swing, proportionally less
  where a position limit clamped it), against 7 units of error for an unloaded
  leg and up to 40 for a neck carrying the head against gravity. A joint parked
  near one of its own limits swings short and is judged tighter, so move it
  nearer the middle of its range before reading a failure there as a fault.
  Per target rather than per run, because distance from the starting
  position also passes a joint that moves once and jams, one that sags away from
  the goal and stays there, and one that moves the right distance the wrong way.
  The joint must also have covered at least 30 units peak to peak, so a swing
  clamped small by a position limit cannot pass on a couple of degrees of
  motion. Short of either, it prints what was commanded, how far off the joint
  was, and where to look — the swing already ran at `Position_P_Gain` 900, so
  what is left is mechanical (jammed linkage, robot resting on the leg) — and
  exits non-zero.

## Usage

Run these from the repository root. Every subcommand opens the serial port
itself, so stop any other app or holder using the same port first.

```bash
# Verify wiring: which IDs answer on the bus?
uv run python scripts/diagnose_servos.py scan
uv run python scripts/diagnose_servos.py scan --port /dev/tty.usbmodem1101 --start 0 --end 252

# Supply / wiring check: one snapshot, or a live loop while re-seating cables
uv run python scripts/diagnose_servos.py power
uv run python scripts/diagnose_servos.py power --watch --per-id

# A servo answers a ping but will not hold torque: inspect, then clear
uv run python scripts/diagnose_servos.py errors
uv run python scripts/diagnose_servos.py recover

# Where is every joint right now, and which ones are reversed? (moves nothing)
uv run python scripts/diagnose_servos.py joints

# Confirm a single servo moves (lift the robot off the ground first)
uv run python scripts/diagnose_servos.py oscillate --id 1
uv run python scripts/diagnose_servos.py oscillate --id 4 --duration 8
```
