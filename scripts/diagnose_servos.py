#!/usr/bin/env python3
"""diagnose_servos — servo bus diagnostics for the Palmimo DevKit.

Subcommands:
  scan       Ping an ID range and list every responding servo with its model number.
  power      Report each servo's input voltage and current (one snapshot, or --watch for a live loop).
  errors     Report each servo's hardware-error status, torque state, and voltage.
  joints     Report each servo's present position, drive mode, torque, temperature, and position gain.
  recover    Reboot the servos whose hardware error has latched, then re-check them.
  oscillate  Swing one servo +/-15 degrees around ITS CURRENT POSITION for assembly checks.

Only `recover` (reboots servos) and `oscillate` (drives a servo) touch state;
the rest are read-only. Every subcommand opens the serial port itself, so stop
any app or holder using the same port first — only one process can hold it.
"""

import argparse
import contextlib
import math
import sys
import time
from typing import NamedTuple

from dynamixel_sdk import COMM_SUCCESS, PacketHandler, PortHandler
from tqdm import tqdm

from palmimo_sdk import PortDetectionError, find_servo_port


PROTOCOL_VERSION = 2.0
DEFAULT_BAUDRATE = 1000000

# Servo control table addresses
ADDR_DRIVE_MODE = 10
ADDR_OPERATING_MODE = 11
ADDR_MAX_POSITION_LIMIT = 48
ADDR_MIN_POSITION_LIMIT = 52
ADDR_TORQUE_ENABLE = 64
ADDR_HARDWARE_ERROR = 70
ADDR_POSITION_P_GAIN = 84
ADDR_PROFILE_ACCELERATION = 108
ADDR_PROFILE_VELOCITY = 112
ADDR_GOAL_POSITION = 116
ADDR_PRESENT_POSITION = 132
ADDR_PRESENT_CURRENT = 126
ADDR_PRESENT_VOLTAGE = 144
ADDR_PRESENT_TEMPERATURE = 146

OPERATING_MODE_POSITION = 3

# Drive_Mode bit positions (see the servo's control table).
DRIVE_MODE_REVERSE_BIT = 0
DRIVE_MODE_TIME_PROFILE_BIT = 2

# The servo IDs the robot is built from.
IDS = list(range(1, 22))

# scan defaults — the full addressable ID range.
DEFAULT_ID_START = 0
DEFAULT_ID_END = 252

# power defaults
CUR_UNIT_MA = 1.0  # the servo's current unit ~1.0 mA/LSB (approximate)
DEFAULT_INTERVAL = 0.4

# recover: time the servos need to re-init after a reboot instruction.
REBOOT_SETTLE_SECONDS = 1.2

# oscillate defaults
DEFAULT_DURATION = 5.0
DXL_CENTER_POSITION = 2048  # the servo's center (0-4095)
TICKS_PER_REV = 4096
UNITS_PER_DEGREE = TICKS_PER_REV / 360
# 15 degrees ~= 170 units at 4096 positions per revolution.
OSCILLATION_AMPLITUDE = 170
# Added to the profiled move time to get the dwell after each commanded target.
# The dwell has to outlast the move, or the next target is commanded before the
# joint arrives and every printed reading describes a joint still in flight; the
# margin on top is what leaves a still frame to look at. Deriving it this way
# keeps both drive modes correct whatever speed the profile below asks for. The
# swing speed was chosen on hardware at 900 ms + 0.35 s = 1.25 s per half-swing.
SWING_SETTLE_SECONDS = 0.35
# Below this the swing is too small to judge by eye, so the check refuses to run
# rather than reporting a joint "fine" after moving it 2 degrees.
MIN_VISIBLE_TRAVEL = 60

# Position_P_Gain the swing is driven at, and the floor the joint has to reach
# for the run to count as an observation.
#
# Position_P_Gain is a RAM register the SDK lowers and leaves low: its shutdown
# park ramps the neck's gain down a step at a time and stops at 6 (see
# `_NECK_RELEASE_GAINS` in palmimo_sdk/robot.py), which is what a fleet read of
# address 84 finds on ids 19-21 after any ordinary session while the legs still
# read 900. A servo on gain 6 accepts every goal and drives none of them, so
# this diagnostic writes the SDK's own power-on default for the duration of the
# swing (`_DEFAULT_POSITION_P_GAIN` in palmimo_sdk/io/dynamixel.py) and puts the
# value it found back on exit -- the same read/write/restore the motion profile
# below already gets.
#
# Change-and-restore rather than refuse-and-explain, deliberately. Against it:
# stiffness is far more user-visible than a profile constant, so changing it is
# a bigger liberty to take. For it: the trigger is the ordinary sequence "run
# the robot, then check the neck", so refusing would make the tool refuse
# exactly the case it exists to diagnose, and the only remedy it could name is
# "write register 84 by hand" -- a worse tool than this one. The liberty is
# bounded on all three sides: the value written is the servo's own default and
# nothing bespoke, both the found and the applied gain are printed rather than
# changed silently, and the joint ends the run with torque off, so no stiffness
# survives it. A reviewer who weighs visibility higher would turn this into a
# refusal naming Position_P_Gain; that is the disagreement, and it is about
# which of the two is the better tool, not about what the register does.
SWING_POSITION_P_GAIN = 900
# A commanded swing that the joint does not follow is the failure this check
# exists to catch, and it is invisible from the bus: every write is accepted and
# every read is honest. So the run is judged on how close the joint got to each
# target it was given -- not on how far it wandered from its starting position,
# which counts any movement, in any direction, at any time (see _report_travel).
# The allowance is a fraction of the excursion commanded from the start.
#
# The fraction has to sit between a loaded joint that legitimately lags and a
# joint that is not being driven at all. Measured on hardware: an unloaded leg
# tracks a +/-170 unit swing to within 7 units, while a neck holding the head
# against gravity at Position_D_Gain 0 stops 27 to 40 units short of each target
# (it reaches 130 to 143 of 170) -- the servo's proportional term settles where
# it balances the gravity torque, and that offset is normal, not a fault. A
# joint that is not driven is a whole excursion away from its target, 170 units.
# Half of the commanded excursion is a wide margin above the worst legitimate
# error measured (a joint would have to lag ~2x worse than the loaded neck
# before this false-fails) and far below what an undriven joint shows.
MIN_TRACKING_FRACTION = 0.5
# Absolute floor, alongside the fraction, on the peak-to-peak distance the joint
# was read over. The allowance above scales with the commanded excursion, and
# that excursion shrinks when a position limit clamps the swing: the narrowest
# window this command will run at all (MIN_VISIBLE_TRAVEL, 60 units peak to
# peak) leaves an allowance of only 15 units, so tracking alone would pass a
# joint that moved 1.3 degrees. This floor is what "the joint moved" means in
# ticks whatever the window: half of MIN_VISIBLE_TRAVEL, which is what a joint
# tracking at the edge of its allowance still shows in that narrowest window, so
# it constrains the clamped swings without touching a full +/-170 one.
MIN_TRACKING_SPAN = MIN_VISIBLE_TRAVEL // 2

# Motion profile for the swing. Without it the servo runs unprofiled at
# Velocity_Limit — a stiff, un-ramped step that shocks the gears.
# Time-based Profile_Velocity is the move time in ms. This is a check a human
# watches to decide which joint a servo drives, so it deliberately moves slower
# than the SDK driver's own connect-time speed (`profile_velocity=300` in
# palmimo_sdk/io/dynamixel.py): 900 ms was picked by eye from four candidates run
# on the robot. A gentler ramp is also the safe direction to err in on a machine
# that was assembled minutes ago.
PROFILE_VELOCITY_MS = 900
# Time-based Profile_Acceleration is the accel/decel ramp in ms; the servo
# requires it to be at most half of Profile_Velocity (300 <= 450). The SDK writes
# only Profile_Velocity at connect, so this register is the diagnostic's own
# choice — which is why it is read and restored rather than left behind.
PROFILE_ACCELERATION_MS = 300
# Velocity-based fallback (Drive_Mode's time-profile bit clear): the two
# registers mean a peak speed and a ramp rate instead of a duration, so the move
# time has to be worked out from the control table. One Profile_Velocity unit is
# ~0.229 rev/min and one Profile_Acceleration unit is ~214.577 rev/min^2; pairing
# velocity with velocity/8 of acceleration mirrors the SDK's
# `_ACCEL_RAMP_DIVISOR` and fixes the ramp at ~0.51 s whatever the velocity.
# `_profile_registers` reports the resulting move time and the dwell follows it,
# so this branch stays correct without a fixed period having to cover both.
DXL_VELOCITY_UNIT_RPM = 0.229
DXL_ACCELERATION_UNIT_RPM_PER_MIN = 214.577
ACCEL_RAMP_DIVISOR = 8

# This bus drops a packet now and then, so one failed read is evidence about the
# wire, not about the servo. Four attempts = the first plus three retries, which
# is what the SDK driver gives its own Present_Position reads (`num_retry=3` in
# palmimo_sdk/io/dynamixel.py counts retries after the first attempt), so a
# flaky wire gets the same number of chances here as it does under the SDK.
READ_ATTEMPTS = 4

# Protocol 2.0 status-packet error byte, bit 7 (`ERRBIT_ALERT` in dynamixel_sdk).
# A servo sets it on EVERY packet it emits while its Hardware_Error_Status is
# non-zero, so it flags a standing condition rather than refusing the packet
# that carried it. The vendor handler masks it off the same way before judging
# an error (`not_alert_error = error & ~ERRBIT_ALERT`).
ERRBIT_ALERT = 0x80

# Goal clamp mirroring the SDK's safe tick range (palmimo_sdk/io/dynamixel.py):
# a servo's own Min/Max_Position_Limit is usually tighter and wins where it is.
SAFE_MIN_TICK = 200
SAFE_MAX_TICK = 3900

# Hardware_Error_Status bit meanings, for readable output.
_HW_BITS = {
    0: "InputVoltage",
    2: "Overheating",
    3: "MotorEncoder",
    4: "ElectricalShock",
    5: "Overload",
}

_DEAD_BUS_MESSAGE = (
    "\n  0/21 respond -> the bus is electrically dead (no power / loose "
    "connection).\n  Reboot CANNOT help here: fix the servo supply/wiring "
    "first, then re-run."
)


def _resolve_port(port: str | None) -> str | None:
    """Return the requested port, or the auto-detected one when none is given."""
    if port is not None:
        return port
    try:
        return find_servo_port()
    except PortDetectionError as exc:
        print(f"Error: {exc}")
        return None


def _open_bus(port_name: str, baudrate: int) -> tuple[PortHandler, PacketHandler] | None:
    """Open the servo bus, or report why it could not be opened."""
    port = PortHandler(port_name)
    if not port.openPort():
        print(f"Error: could not open {port_name} (holder/example still has it?)")
        return None
    if not port.setBaudRate(baudrate):
        print(f"Error: failed to set baudrate {baudrate}")
        port.closePort()
        return None
    return port, PacketHandler(PROTOCOL_VERSION)


def _s16(value: int) -> int:
    """Reinterpret a 2-byte register value as signed."""
    return value - 65536 if value >= 32768 else value


def _s32(value: int) -> int:
    """Reinterpret a 4-byte register value as signed.

    Present_Position is a signed register (the SDK lists it in
    ``SIGNED_REGISTERS``): with a non-zero Homing_Offset a joint just below zero
    comes back as ~4.29e9 unless it is decoded this way.
    """
    return value - 4294967296 if value >= 2147483648 else value


def _err_names(flags: int) -> str:
    names = [name for bit, name in _HW_BITS.items() if flags & (1 << bit)]
    return ",".join(names) if names else "-"


def _drive_mode_names(flags: int) -> str:
    """Describe a Drive_Mode value: rotation direction and profile kind."""
    direction = "reverse" if flags & (1 << DRIVE_MODE_REVERSE_BIT) else "normal"
    profile = "time-based" if flags & (1 << DRIVE_MODE_TIME_PROFILE_BIT) else "velocity-based"
    return f"{direction},{profile}"


def scan_dynamixel(port: str, baudrate: int, id_start: int, id_end: int) -> list[tuple[int, int]]:
    """Return the ID and model number of each responding servo."""
    port_handler = PortHandler(port)
    packet_handler = PacketHandler(PROTOCOL_VERSION)

    if not port_handler.openPort():
        print(f"Error: Failed to open port {port}")
        sys.exit(1)

    if not port_handler.setBaudRate(baudrate):
        print(f"Error: Failed to set baudrate {baudrate}")
        port_handler.closePort()
        sys.exit(1)

    print(f"Scanning for DYNAMIXEL servos on {port} at {baudrate} baud...")
    print(f"ID range: {id_start} - {id_end}")

    found_ids: list[tuple[int, int]] = []

    # stderr, not stdout: tqdm draws the bar on stderr, so that is the stream
    # whose interactivity decides whether the redraws are readable.
    with tqdm(
        range(id_start, id_end + 1),
        desc="Scanning",
        unit="ID",
        disable=not sys.stderr.isatty(),
    ) as pbar:
        for dxl_id in pbar:
            # Ping to check if servo exists
            model_number, result, _ = packet_handler.ping(port_handler, dxl_id)

            if result == 0:  # COMM_SUCCESS
                found_ids.append((dxl_id, model_number))
                pbar.set_postfix(found=len(found_ids), last_id=dxl_id)

    port_handler.closePort()
    return found_ids


def _read_power(port: PortHandler, ph: PacketHandler) -> tuple[dict[int, float], dict[int, float], list[int]]:
    """Return per-ID voltage, per-ID current, and the IDs that did not answer."""
    volts: dict[int, float] = {}
    curr: dict[int, float] = {}
    miss: list[int] = []
    for i in IDS:
        v, cr, _ = ph.read2ByteTxRx(port, i, ADDR_PRESENT_VOLTAGE)
        if cr != 0:
            miss.append(i)
            continue
        volts[i] = v / 10.0
        c, cr2, _ = ph.read2ByteTxRx(port, i, ADDR_PRESENT_CURRENT)
        if cr2 == 0:
            curr[i] = abs(_s16(c)) * CUR_UNIT_MA
    return volts, curr, miss


def _read_error_status(port: PortHandler, ph: PacketHandler) -> dict[int, tuple[int | None, int | None, float | None]]:
    """Return {id: (hwerr, torque, volts)} for responders; missing ids absent."""
    found: dict[int, tuple[int | None, int | None, float | None]] = {}
    for i in IDS:
        _, cr, _ = ph.ping(port, i)
        if cr != 0:
            continue
        hw, c1, _ = ph.read1ByteTxRx(port, i, ADDR_HARDWARE_ERROR)
        tq, c2, _ = ph.read1ByteTxRx(port, i, ADDR_TORQUE_ENABLE)
        v, c3, _ = ph.read2ByteTxRx(port, i, ADDR_PRESENT_VOLTAGE)
        found[i] = (
            hw if c1 == 0 else None,
            tq if c2 == 0 else None,
            (v / 10.0) if c3 == 0 else None,
        )
    return found


def _print_error_report(port_name: str, found: dict[int, tuple[int | None, int | None, float | None]]) -> list[int]:
    """Print one line per servo and return the IDs with a latched hardware error."""
    print(f"=== scan on {port_name} ===  responders: {len(found)}/21")
    errored: list[int] = []
    for i in IDS:
        if i not in found:
            print(f"  id{i:2d}: NO-RESPONSE")
            continue
        hw, tq, v = found[i]
        hw_s = f"0x{hw:02x}({_err_names(hw)})" if hw is not None else "?"
        flag = "  <== HW ERROR" if (hw is not None and hw != 0) else ""
        if hw is not None and hw != 0:
            errored.append(i)
        print(f"  id{i:2d}: hwerr={hw_s}  torque={tq}  vin={v if v is not None else '?'}V{flag}")
    return errored


class JointState(NamedTuple):
    """One servo's read-only snapshot, as the `joints` subcommand reports it.

    Every field is ``None`` when that register did not answer, so a partly
    responsive servo still prints the values it did return.
    """

    position: int | None
    drive_mode: int | None
    torque: int | None
    temperature: int | None
    limits: tuple[int, int] | None
    # Position_P_Gain decides whether a commanded goal is driven at all, so it
    # belongs in the report that answers "why did nothing happen": the SDK's
    # shutdown park leaves the neck servos on a gain of 6 while the legs stay at
    # 900, and a servo on 6 accepts every goal and follows none of them. Reading
    # it here is what lets one servo's gain be compared against its peers'.
    p_gain: int | None


def _read_joint_states(port: PortHandler, ph: PacketHandler) -> dict[int, JointState]:
    """Return {id: JointState} for responders; missing ids absent. Reads only."""
    found: dict[int, JointState] = {}
    for i in IDS:
        _, cr, _ = ph.ping(port, i)
        if cr != COMM_SUCCESS:
            continue
        pos, c1, _ = ph.read4ByteTxRx(port, i, ADDR_PRESENT_POSITION)
        drive, c2, _ = ph.read1ByteTxRx(port, i, ADDR_DRIVE_MODE)
        tq, c3, _ = ph.read1ByteTxRx(port, i, ADDR_TORQUE_ENABLE)
        temp, c4, _ = ph.read1ByteTxRx(port, i, ADDR_PRESENT_TEMPERATURE)
        lo, c5, _ = ph.read4ByteTxRx(port, i, ADDR_MIN_POSITION_LIMIT)
        hi, c6, _ = ph.read4ByteTxRx(port, i, ADDR_MAX_POSITION_LIMIT)
        gain, c7, _ = ph.read2ByteTxRx(port, i, ADDR_POSITION_P_GAIN)
        found[i] = JointState(
            position=_s32(pos) if c1 == COMM_SUCCESS else None,
            drive_mode=drive if c2 == COMM_SUCCESS else None,
            torque=tq if c3 == COMM_SUCCESS else None,
            temperature=temp if c4 == COMM_SUCCESS else None,
            limits=(lo, hi) if (c5 == COMM_SUCCESS and c6 == COMM_SUCCESS) else None,
            p_gain=gain if c7 == COMM_SUCCESS else None,
        )
    return found


def _print_joint_report(port_name: str, found: dict[int, JointState]) -> None:
    """Print one line per servo: where the joint is and how it is configured."""
    print(f"=== joints on {port_name} ===  responders: {len(found)}/21")
    for i in IDS:
        if i not in found:
            print(f"  id{i:2d}: NO-RESPONSE")
            continue
        state = found[i]
        if state.position is None:
            pos_s = "pos=?"
        else:
            degrees = (state.position - DXL_CENTER_POSITION) / UNITS_PER_DEGREE
            pos_s = f"pos={state.position:4d} ({degrees:+7.2f} deg from center)"
        drive = state.drive_mode
        drive_s = f"0x{drive:02x}({_drive_mode_names(drive)})" if drive is not None else "?"
        limits_s = f"[{state.limits[0]},{state.limits[1]}]" if state.limits else "?"
        temp_s = f"{state.temperature}C" if state.temperature is not None else "?"
        torque_s = str(state.torque) if state.torque is not None else "?"
        gain_s = str(state.p_gain) if state.p_gain is not None else "?"
        print(
            f"  id{i:2d}: {pos_s}  drive={drive_s}  torque={torque_s}  temp={temp_s}  limits={limits_s}  pgain={gain_s}"
        )


def _print_safely(text: str) -> None:
    """Print without ever raising, for use on a path that must not be interrupted.

    ``print`` itself fails in two ways this tool reaches: ``BrokenPipeError``
    when stdout is a closed pipe (``... | head -5``), and ``UnicodeEncodeError``
    when the console encoding cannot carry the text. Neither may stop a torque
    release or replace the exception already on its way out.
    """
    with contextlib.suppress(Exception):
        print(text)


class _AlertLatch:
    """Records the Protocol 2.0 alert bit, reporting a latched servo once per run.

    A latched servo answers normally and obeys writes; it just sets
    :data:`ERRBIT_ALERT` on every packet. Judging that byte without masking the
    bit turns each of those successful transactions into a reported failure —
    including the torque release the operator relies on — so the bit is
    accounted for here instead: once, as the standing hardware-error condition
    it is, and as the reason the run stops.
    """

    def __init__(self) -> None:
        self.tripped = False

    def note(self, servo_id: int, error: int) -> None:
        """Report a latched hardware error the first time *error* carries the alert bit."""
        if not error & ERRBIT_ALERT or self.tripped:
            return
        self.tripped = True
        print(
            f"Servo {servo_id} has a latched hardware error: every packet it sends carries the alert bit. "
            "Its readings and its motion cannot be trusted, so this check stops. Run "
            "'diagnose_servos.py errors' to see which error latched, then 'diagnose_servos.py recover' "
            "to clear it."
        )


def _write_register(
    ph: PacketHandler,
    port: PortHandler,
    servo_id: int,
    address: int,
    value: int,
    size: int,
    what: str,
    alert: _AlertLatch,
) -> bool:
    """Write one register and report the failure instead of raising.

    Args:
        ph: Packet handler for the open bus.
        port: Port handler for the open bus.
        servo_id: Dynamixel servo ID.
        address: Control-table address to write.
        value: Value to write.
        size: Register width in bytes (1, 2, or 4).
        what: What the write was for, used in the failure message.
        alert: Latch that records a servo reporting a latched hardware error.

    Returns:
        Whether the servo carried out the write. An error byte carrying only
        the alert bit means it did: that bit is recorded in *alert*, which is
        what ends the run, rather than being blamed on this write.
    """
    writer = {1: ph.write1ByteTxRx, 2: ph.write2ByteTxRx, 4: ph.write4ByteTxRx}[size]
    comm, error = writer(port, servo_id, address, value)
    if comm != COMM_SUCCESS:
        print(f"Failed to {what}: {ph.getTxRxResult(comm)}")
        return False
    alert.note(servo_id, error)
    rejection = error & ~ERRBIT_ALERT
    if rejection:
        # Protocol 2.0 reports a REJECTED write in the status packet's error byte
        # while comm still says success — a goal outside Min/Max_Position_Limit
        # comes back this way. Ignoring it makes a servo that refused every goal
        # look like a clean run. The alert bit is masked out of both the test and
        # the message, or `getRxPacketError` would describe the latched hardware
        # error instead of the reason this write was refused.
        print(f"Failed to {what}: the servo rejected it ({ph.getRxPacketError(rejection)})")
        return False
    return True


def _read_register(
    ph: PacketHandler, port: PortHandler, servo_id: int, address: int, size: int, what: str, alert: _AlertLatch
) -> int | None:
    """Read one register, retrying dropped packets, and report a real failure.

    Args:
        ph: Packet handler for the open bus.
        port: Port handler for the open bus.
        servo_id: Dynamixel servo ID.
        address: Control-table address to read.
        size: Register width in bytes (1, 2, or 4).
        what: Register name, used in the failure message.
        alert: Latch that records a servo reporting a latched hardware error.

    Returns:
        The register value, or ``None`` when every attempt failed. A latched
        servo still returns its value: the alert bit is recorded in *alert*, not
        treated as a failed read.
    """
    reader = {1: ph.read1ByteTxRx, 2: ph.read2ByteTxRx, 4: ph.read4ByteTxRx}[size]
    comm = COMM_SUCCESS
    error = 0
    for _attempt in range(READ_ATTEMPTS):
        value, comm, error = reader(port, servo_id, address)
        if comm == COMM_SUCCESS and (error & ~ERRBIT_ALERT) == 0:
            alert.note(servo_id, error)
            return int(value)
    alert.note(servo_id, error)
    detail = ph.getTxRxResult(comm) if comm != COMM_SUCCESS else ph.getRxPacketError(error & ~ERRBIT_ALERT)
    print(f"Failed to read {what} from servo {servo_id} after {READ_ATTEMPTS} attempts: {detail}")
    return None


def _read_position_window(
    ph: PacketHandler, port: PortHandler, servo_id: int, alert: _AlertLatch
) -> tuple[int, int] | None:
    """Return the tick window a goal may use, or ``None`` when it cannot be established.

    A unit can carry limits far tighter than the SDK's documented 200-3900 (one
    measured robot reports 1023/3073), so the servo's registers decide and the
    safe range is only the outer bound. An unreadable limit is refused rather
    than filled in from the safe range: on real hardware that fallback is WIDER
    than the true limits, so it would hand out goals the servo rejects and the
    swing would silently never happen.

    The window returned is always non-empty. Both the servo's own limits and the
    result of narrowing them to the safe range are checked, because a servo
    whose limits lie wholly outside 200-3900 passes the first check and fails
    the second — and an inverted window handed to the caller would be printed
    back to the operator as a pair of ticks the servo never reported.
    """
    lo = _read_register(ph, port, servo_id, ADDR_MIN_POSITION_LIMIT, 4, "Min_Position_Limit", alert)
    hi = _read_register(ph, port, servo_id, ADDR_MAX_POSITION_LIMIT, 4, "Max_Position_Limit", alert)
    if lo is None or hi is None:
        return None
    if lo >= hi:
        print(f"Servo {servo_id} reports Min_Position_Limit {lo} >= Max_Position_Limit {hi}, which cannot be right.")
        return None
    low, high = max(SAFE_MIN_TICK, lo), min(SAFE_MAX_TICK, hi)
    if low >= high:
        print(
            f"Servo {servo_id}'s position limits [{lo}, {hi}] lie outside the safe tick range "
            f"[{SAFE_MIN_TICK}, {SAFE_MAX_TICK}], so there is no goal this check may command."
        )
        return None
    return low, high


def _swing_window(present: int, low_limit: int, high_limit: int) -> tuple[int, int]:
    """Return the (low, high) swing targets centered on *present*, clamped to the limits.

    Centering on the present position is what keeps the first commanded step to
    the amplitude at most, whatever pose the robot happens to be in.
    """
    return (
        max(low_limit, present - OSCILLATION_AMPLITUDE),
        min(high_limit, present + OSCILLATION_AMPLITUDE),
    )


class SwingProfile(NamedTuple):
    """The profile registers for one ramped swing, and the move time they imply.

    ``move_seconds`` is what the dwell is built from, so the loop can never wait
    less than the move it just commanded — whatever speed
    :data:`PROFILE_VELOCITY_MS` asks for and whichever drive mode the servo is in.
    """

    acceleration: int
    velocity: int
    move_seconds: float


def _velocity_profile_seconds(velocity: int, acceleration: int) -> float:
    """Return how long a full-amplitude swing takes under the velocity-based profile.

    The register pair sets a peak speed and a ramp rate, so the move is
    trapezoidal when the two ramps fit inside the travel and triangular — peak
    speed never reached — when they do not. Which case applies depends on the
    speed asked for, so both are computed rather than assumed.

    Args:
        velocity: Profile_Velocity in ~0.229 rev/min units.
        acceleration: Profile_Acceleration in ~214.577 rev/min^2 units.

    Returns:
        Seconds to cross ``2 * OSCILLATION_AMPLITUDE`` ticks. A clamped swing is
        shorter than that, so this is the upper bound over one run.
    """
    peak_rev_per_second = velocity * DXL_VELOCITY_UNIT_RPM / 60.0
    accel_rev_per_second2 = acceleration * DXL_ACCELERATION_UNIT_RPM_PER_MIN / 3600.0
    travel_rev = (2 * OSCILLATION_AMPLITUDE) / TICKS_PER_REV
    ramp_seconds = peak_rev_per_second / accel_rev_per_second2
    # Accelerating to the peak and back down again covers this much on its own.
    ramp_rev = peak_rev_per_second * ramp_seconds
    if ramp_rev >= travel_rev:
        return 2.0 * math.sqrt(travel_rev / accel_rev_per_second2)
    return 2.0 * ramp_seconds + (travel_rev - ramp_rev) / peak_rev_per_second


def _profile_registers(time_based: bool) -> SwingProfile:
    """Return the profile for one ramped swing, with the move time it implies.

    Args:
        time_based: Whether Drive_Mode's time-profile bit is set, which changes
            what both registers mean.

    Returns:
        A :class:`SwingProfile` giving a ramped move in either drive mode. Under
        the time-based profile Profile_Velocity IS the move time, so it is
        :data:`PROFILE_VELOCITY_MS` exactly; under the velocity-based one the
        same speed is expressed as a peak and a ramp, which works out slower.
        Both are far gentler than the unprofiled step they replace.
    """
    if time_based:
        return SwingProfile(PROFILE_ACCELERATION_MS, PROFILE_VELOCITY_MS, PROFILE_VELOCITY_MS / 1000.0)
    ticks_per_second = (2 * OSCILLATION_AMPLITUDE) / (PROFILE_VELOCITY_MS / 1000.0)
    rev_per_min = (ticks_per_second / TICKS_PER_REV) * 60.0
    velocity = max(1, round(rev_per_min / DXL_VELOCITY_UNIT_RPM))
    acceleration = max(1, round(velocity / ACCEL_RAMP_DIVISOR))
    return SwingProfile(acceleration, velocity, _velocity_profile_seconds(velocity, acceleration))


def _swing_period_seconds(profile: SwingProfile) -> float:
    """Return the dwell after each commanded target: the profiled move plus settle."""
    return profile.move_seconds + SWING_SETTLE_SECONDS


def _read_profile(ph: PacketHandler, port: PortHandler, servo_id: int, alert: _AlertLatch) -> tuple[int, int] | None:
    """Return the servo's current (Profile_Acceleration, Profile_Velocity), or ``None`` if unreadable."""
    accel = _read_register(ph, port, servo_id, ADDR_PROFILE_ACCELERATION, 4, "Profile_Acceleration", alert)
    velocity = _read_register(ph, port, servo_id, ADDR_PROFILE_VELOCITY, 4, "Profile_Velocity", alert)
    if accel is None or velocity is None:
        return None
    return accel, velocity


def _restore_profile(
    ph: PacketHandler,
    port: PortHandler,
    servo_id: int,
    previous_profile: tuple[int, int] | None,
    alert: _AlertLatch,
) -> None:
    """Put the profile registers back as they were found, saying so when it cannot be done.

    These are RAM registers other tools read and write, and the SDK rewrites only
    Profile_Velocity at connect — a Profile_Acceleration left behind here would
    survive every later session until a power cycle, as one leg quietly moving
    unlike its five peers.
    """
    if previous_profile is None:
        print(
            f"Warning: servo {servo_id}'s original profile was never read, so this run's "
            "Profile_Acceleration/Profile_Velocity are still in place. Power-cycle the robot before running the "
            "SDK, or that joint will move unlike its peers."
        )
        return
    _write_register(
        ph, port, servo_id, ADDR_PROFILE_ACCELERATION, previous_profile[0], 4, "restore Profile_Acceleration", alert
    )
    _write_register(
        ph, port, servo_id, ADDR_PROFILE_VELOCITY, previous_profile[1], 4, "restore Profile_Velocity", alert
    )


def _restore_position_p_gain(
    ph: PacketHandler,
    port: PortHandler,
    servo_id: int,
    previous_gain: int | None,
    torque_enabled: bool,
    alert: _AlertLatch,
) -> None:
    """Put Position_P_Gain back as it was found, saying so when it cannot be done.

    Unlike Profile_Acceleration, a gain left behind here is the servo's own
    power-on default and the SDK re-applies exactly that value at connect, so
    failing to restore it does not strand the robot in a state a power cycle is
    needed to leave. It is still reported: a joint the operator left deliberately
    soft comes back stiff until the next connect, and that is theirs to know.

    Args:
        ph: Vendor packet handler.
        port: Vendor port handler.
        servo_id: Dynamixel servo ID.
        previous_gain: The gain read before the swing, or ``None`` if unreadable.
        torque_enabled: Whether the joint may still be powered, i.e. the torque
            release was refused. A restore that RAISES the gain would then stiffen
            a live joint, so it is skipped and reported instead.
        alert: Latch collecting hardware-error alerts.
    """
    if previous_gain is None:
        print(
            f"Note: servo {servo_id}'s original Position_P_Gain was never read, so this run's value "
            f"({SWING_POSITION_P_GAIN}) was left on it. That is the servo's power-on default, which the SDK "
            "re-applies at connect, so no power cycle is needed to clear it."
        )
        return
    if torque_enabled and previous_gain > SWING_POSITION_P_GAIN:
        # Lowering the gain on a still-powered joint only softens it, so only the
        # raising direction is held back here.
        print(
            f"Note: servo {servo_id}'s original Position_P_Gain ({previous_gain}) is above the "
            f"{SWING_POSITION_P_GAIN} this run used, and the joint did not accept the torque release, so writing it "
            f"back would stiffen a joint that may still be powered. Position_P_Gain is left at "
            f"{SWING_POSITION_P_GAIN}; the SDK re-applies its own default at connect."
        )
        return
    _write_register(ph, port, servo_id, ADDR_POSITION_P_GAIN, previous_gain, 2, "restore Position_P_Gain", alert)


def _report_travel(servo_id: int, commanded_peak: int, worst_error: int, observed_span: int) -> bool:
    """Judge whether the joint followed the swing, explaining a failure to follow.

    The verdict is per target -- how far the joint was from the target it had
    just been given -- rather than how far it ever got from its starting
    position. Distance from the start counts any movement, in any direction, at
    any time, so it passes three joints that never followed anything: one that
    tracks the first target and then jams, one that sags away from the first goal
    at torque-on and stays there (the proportional-controller offset shape this
    check exists to catch), and one that moves the right distance the wrong way
    on every target. Comparing the reading with its own target rejects all three,
    and is the claim the command actually makes. Requiring a signed extreme on
    each side instead would reject the first two but not the third: a joint that
    moves 100 units the wrong way on every target still reaches both extremes.

    Args:
        servo_id: Dynamixel servo ID.
        commanded_peak: Largest excursion commanded from the starting position, in ticks.
        worst_error: Largest distance between a target and where the joint was
            read at the end of that target's dwell, in ticks.
        observed_span: Peak-to-peak distance the joint was read over, in ticks.

    Returns:
        Whether the joint followed the swing. The caller reaches this only after
        a swing that ran end to end, so every write was accepted and every
        reading is honest: a joint that fails here failed because it was not
        driven or could not move, never because the bus failed.
    """
    allowance = round(commanded_peak * (1 - MIN_TRACKING_FRACTION))
    if worst_error <= allowance:
        if observed_span >= MIN_TRACKING_SPAN:
            return True
        print(
            f"\nServo {servo_id} accepted every command and stayed near each target, but the joint only covered "
            f"{observed_span} units ({observed_span / UNITS_PER_DEGREE:.1f} deg) peak to peak -- under the "
            f"{MIN_TRACKING_SPAN} units this check needs before it will call the joint moved. This joint's position "
            "limits clamped the swing down to where tracking it proves nothing. Move the joint nearer the middle of "
            "its range and re-run."
        )
        return False
    print(
        f"\nServo {servo_id} accepted every command but the joint did not follow it: commanded "
        f"+/-{commanded_peak} units ({commanded_peak / UNITS_PER_DEGREE:.1f} deg) from the starting position, "
        f"and after one of those targets the joint was read {worst_error} units "
        f"({worst_error / UNITS_PER_DEGREE:.1f} deg) away from it -- past the {allowance} units this check allows "
        f"({1 - MIN_TRACKING_FRACTION:.0%} of the commanded excursion)."
    )
    print(
        f"  The swing was driven at Position_P_Gain {SWING_POSITION_P_GAIN}, the SDK's own default, so a low gain "
        "is not the cause. If the joint sits near one of its own position limits, the swing was short and the "
        "allowance with it -- move the joint nearer the middle of its range and re-run before reading anything "
        "into this. Otherwise the joint is not free to move: the linkage may be jammed, or the robot may be "
        "resting on that leg. Lift the robot clear and re-run."
    )
    return False


def _release_torque_quietly(ph: PacketHandler, port: PortHandler, servo_id: int, alert: _AlertLatch) -> None:
    """Disable torque without ever raising, for use while another error propagates.

    ``BaseException``, and a print that cannot raise: this runs on paths that
    exist only to make sure the joint ends up limp, so a second Ctrl+C or a
    closed stdout must not be able to skip past it.
    """
    try:
        _write_register(ph, port, servo_id, ADDR_TORQUE_ENABLE, 0, 1, "disable torque", alert)
    except BaseException as exc:
        _print_safely(f"Warning: could not disable torque on servo {servo_id}: {exc}")


def oscillate_servo(servo_id: int, port: str, baudrate: int, duration: float) -> bool:
    """Swing one servo +/-15 degrees around where the joint currently sits.

    The swing is centered on the present position, clamped to the servo's own
    position limits, and ramped by an explicit motion profile written before any
    goal — so the check works from any pose without the abrupt, unprofiled jump
    to a fixed center that a freshly powered robot would otherwise take.

    Args:
        servo_id: Dynamixel servo ID.
        port: Serial port path.
        baudrate: Communication baud rate.
        duration: Oscillation duration in seconds.

    Returns:
        Whether the swing ran end to end AND the joint followed it: every
        commanded target acknowledged, the joint read back after each one and
        found within the tracking allowance of it (:data:`MIN_TRACKING_FRACTION`,
        with :data:`MIN_TRACKING_SPAN` as an absolute floor on the ground
        covered), torque released at the end, and no latched hardware error seen.
        A refusal, a rejected write, a servo that latched, a joint that did not
        follow its targets, and Ctrl+C all report failure.
    """
    # Initialize PortHandler and PacketHandler
    port_handler = PortHandler(port)
    packet_handler = PacketHandler(PROTOCOL_VERSION)

    # Open port
    if not port_handler.openPort():
        print(f"Failed to open port {port}")
        return False

    # Set baudrate
    if not port_handler.setBaudRate(baudrate):
        print(f"Failed to set baudrate to {baudrate}")
        port_handler.closePort()
        return False

    print("\n=== Dynamixel Servo Oscillation Test ===")
    print(f"Servo ID: {servo_id}")
    print(f"Port: {port}")
    print(f"Baudrate: {baudrate}")
    print(f"Amplitude: +/-15 degrees (+/-{OSCILLATION_AMPLITUDE} units) around the present position")
    print(f"Duration: {duration} seconds")
    print("=" * 40 + "\n")

    start_position: int | None = None
    previous_profile: tuple[int, int] | None = None
    previous_gain: int | None = None
    # Derived from the profile once Drive_Mode is known. Every path that waits on
    # it runs after a goal has been commanded, which is after that point.
    swing_period = 0.0
    # The profile values this run writes, named in the exit path's warning. Set
    # with the profile below; the exit path reports them only once they are.
    accel = velocity = 0
    wrote_profile = False
    wrote_gain = False
    torque_enabled = False
    success = False
    alert = _AlertLatch()
    try:
        # Everything the plan depends on is read BEFORE the first write, so any
        # read that fails ends the run with the servo exactly as it was found.
        torque = _read_register(packet_handler, port_handler, servo_id, ADDR_TORQUE_ENABLE, 1, "Torque_Enable", alert)
        if torque is None:
            print(
                f"Without Torque_Enable there is no way to tell whether servo {servo_id} is holding a pose, so "
                "nothing was written and the servo is untouched. Re-run; if it keeps failing, check the bus wiring."
            )
            return False
        mode = _read_register(packet_handler, port_handler, servo_id, ADDR_OPERATING_MODE, 1, "Operating_Mode", alert)
        if mode is None:
            print(
                f"Without Operating_Mode there is no telling whether servo {servo_id} needs switching to position "
                "control, so nothing was written and the servo is untouched. Re-run; if it keeps failing, check the "
                "bus wiring."
            )
            return False
        # Drive_Mode decides what Profile_Velocity/Profile_Acceleration MEAN, and
        # the two readings are far apart in speed: the time-based value 900 means
        # "900 ms per move", while the same register read as velocity units means
        # most of Velocity_Limit. There is no value that is gentle under both, so
        # an unreadable Drive_Mode ends the run instead of guessing.
        drive_mode = _read_register(packet_handler, port_handler, servo_id, ADDR_DRIVE_MODE, 1, "Drive_Mode", alert)
        if drive_mode is None:
            print(
                "Without Drive_Mode there is no motion profile that is safe in both drive modes, so nothing was "
                "written and the servo is untouched. Re-run; if it keeps failing, check the bus wiring."
            )
            return False
        raw_present = _read_register(
            packet_handler, port_handler, servo_id, ADDR_PRESENT_POSITION, 4, "Present_Position", alert
        )
        if raw_present is None:
            print(
                f"Without Present_Position there is nothing to center the swing on, so nothing was written and "
                f"servo {servo_id} is untouched. Re-run; if it keeps failing, check the bus wiring."
            )
            return False
        present = _s32(raw_present)

        window = _read_position_window(packet_handler, port_handler, servo_id, alert)
        if window is None:
            print(
                f"Without servo {servo_id}'s position limits the swing cannot be clamped, so nothing was written. "
                "Re-run; if it keeps failing, check the bus wiring."
            )
            return False
        # A servo whose hardware error has latched has already been reported by
        # the latch. Stopping here, before the first write, leaves it as found.
        if alert.tripped:
            return False
        low_limit, high_limit = window
        low, high = _swing_window(present, low_limit, high_limit)
        # A joint pushed past its own limit cannot be commanded to exactly where
        # it sits (the servo rejects the goal), so hold the nearest legal tick.
        start_position = min(max(present, low_limit), high_limit)
        travel = high - low
        if travel < MIN_VISIBLE_TRAVEL:
            # The window printed here is the servo's own limits narrowed to the
            # safe tick range, so it is named for what it is rather than quoted
            # back as the servo's registers.
            if travel < 0:
                print(
                    f"Servo {servo_id} sits at {present}, outside the usable range [{low_limit}, {high_limit}] "
                    f"(its position limits, narrowed to the safe tick range): no reachable target is within "
                    f"{OSCILLATION_AMPLITUDE} units of it. Move the joint back inside its range and re-run."
                )
            else:
                print(
                    f"Servo {servo_id} sits at {present}, which leaves only {travel} units of travel inside the "
                    f"usable range [{low_limit}, {high_limit}] (its position limits, narrowed to the safe tick "
                    "range): too little to judge by eye. Move the joint away from its limit and re-run."
                )
            return False

        time_based = bool(drive_mode & (1 << DRIVE_MODE_TIME_PROFILE_BIT))
        profile = _profile_registers(time_based)
        accel, velocity = profile.acceleration, profile.velocity
        swing_period = _swing_period_seconds(profile)
        previous_profile = _read_profile(packet_handler, port_handler, servo_id, alert)
        if previous_profile is None:
            print(
                f"Warning: servo {servo_id}'s current profile could not be read, so the values written below cannot "
                "be put back on exit."
            )
        # Read with the profile, and for the same reason: it is a register this
        # run overwrites, so its old value has to be in hand before the first
        # write. An unreadable gain does not stop the run -- the value that
        # would be left behind is the servo's own default (see
        # SWING_POSITION_P_GAIN) -- but it is reported on exit.
        previous_gain = _read_register(
            packet_handler, port_handler, servo_id, ADDR_POSITION_P_GAIN, 2, "Position_P_Gain", alert
        )

        if torque:
            print(
                f"WARNING: servo {servo_id} is holding torque right now, and this check leaves it OFF when it "
                "finishes, so the joint will go limp. Nothing here waits for you: the release happens seconds from "
                "now. Support the robot BEFORE running this command."
            )

        # Operating_Mode lives in EEPROM: the write is rejected while torque is
        # on, and the register has a finite write endurance, so torque is dropped
        # (and the mode written) only when the servo is not already in position
        # control. Skipping it is what keeps a standing robot standing.
        if mode != OPERATING_MODE_POSITION:
            if torque:
                print("         Torque is released now, without a pause, to switch this servo to position control.")
            if not _write_register(
                packet_handler, port_handler, servo_id, ADDR_TORQUE_ENABLE, 0, 1, "disable torque", alert
            ):
                return False
            if not _write_register(
                packet_handler,
                port_handler,
                servo_id,
                ADDR_OPERATING_MODE,
                OPERATING_MODE_POSITION,
                1,
                "set position mode",
                alert,
            ):
                return False

        # The profile goes in BEFORE any goal, so no commanded move is ever
        # unprofiled. Acceleration first, matching the SDK driver's write order.
        # The flag is set here and nowhere earlier: it decides whether the exit
        # path has a profile to put back, and the writes above leave none.
        wrote_profile = True
        if not _write_register(
            packet_handler,
            port_handler,
            servo_id,
            ADDR_PROFILE_ACCELERATION,
            accel,
            4,
            "set profile acceleration",
            alert,
        ):
            return False
        if not _write_register(
            packet_handler, port_handler, servo_id, ADDR_PROFILE_VELOCITY, velocity, 4, "set profile velocity", alert
        ):
            return False

        # Seed the goal with where the joint already is, so enabling torque holds
        # the pose instead of snapping to whatever goal the servo still carried.
        if not _write_register(
            packet_handler, port_handler, servo_id, ADDR_GOAL_POSITION, start_position, 4, "seed goal position", alert
        ):
            return False
        # Gain AFTER the goal seed, and the seed is the safety property here.
        #
        # Position_P_Gain acts through the servo's position controller, on the
        # error between Goal_Position and where the joint is. The invariant this
        # order establishes is that the goal already holds the present position
        # by the time the gain goes up, so there is no error for a raised gain to
        # act on -- the strongest thing it can then do is hold the joint where it
        # already is.
        #
        # Torque may well be ON at this point: it is dropped above only when the
        # servo was not already in position control, so on the ordinary path this
        # write reaches a live controller immediately rather than waiting for the
        # enable below. That is safe for the same reason, and only for that
        # reason -- the goal was seeded one statement earlier.
        #
        # Raising it in either other position is what snaps the head. Before the
        # goal seed, on a servo that is ALREADY holding torque -- the neck on
        # gain 6 has drooped away from whatever goal the last session left in the
        # register, and multiplying that standing error by 150 hauls the head to
        # it at full speed. After the torque enable, on a servo that was not, the
        # joint is held at gain 6 while it sags, and the raise then yanks the sag
        # back out.
        #
        # The flag is set before the write for the same reason as torque below:
        # a write that raises or is only partly carried out still has to be
        # undone by the exit path.
        wrote_gain = True
        if not _write_register(
            packet_handler, port_handler, servo_id, ADDR_POSITION_P_GAIN, SWING_POSITION_P_GAIN, 2, "set gain", alert
        ):
            return False
        # Set before the write, not after: if the enable raises or is only
        # partly carried out, the error paths must still know to release.
        torque_enabled = True
        if not _write_register(
            packet_handler, port_handler, servo_id, ADDR_TORQUE_ENABLE, 1, 1, "enable torque", alert
        ):
            return False

        profile_kind = "time-based" if time_based else "velocity-based"
        print(f"Center (present position): {present}   limits: [{low_limit}, {high_limit}]")
        print(f"Swing: {low} .. {high}  ({travel / UNITS_PER_DEGREE:.1f} deg peak to peak)")
        print(f"Profile: velocity={velocity} acceleration={accel} ({profile_kind})")
        found_gain = str(previous_gain) if previous_gain is not None else "unreadable"
        print(
            f"Position_P_Gain: found {found_gain}, driving this swing at {SWING_POSITION_P_GAIN}, put back on exit "
            "(a restore that cannot be done is reported)"
        )
        print(f"Move: {profile.move_seconds:.2f} s per swing, held {swing_period:.2f} s before each reading")
        print("Torque enabled. Starting oscillation...\n")

        # Start on whichever side has more room, so the first swing is visible
        # even when a limit clamped the other side down to almost nothing.
        first, second = (high, low) if (high - present) >= (present - low) else (low, high)
        swings = max(2, round(duration / swing_period))
        swing_ok = True
        # What the joint was asked for, how far it ended up from the target it
        # was given, and how much ground it covered doing so. Only targets the
        # servo acknowledged are counted, so a run that broke off early is not
        # judged against a goal it never got.
        commanded_peak = 0
        worst_error = 0
        offsets: list[int] = []
        for index in range(swings):
            target = first if index % 2 == 0 else second
            if not _write_register(
                packet_handler, port_handler, servo_id, ADDR_GOAL_POSITION, target, 4, "write goal position", alert
            ):
                swing_ok = False
                break
            commanded_peak = max(commanded_peak, abs(target - present))

            # Read AFTER the dwell, not before it: reading first reports where the
            # joint was when the PREVIOUS target was commanded, so every printed
            # line would trail the swing it claims to describe.
            time.sleep(swing_period)
            reached = _read_register(
                packet_handler, port_handler, servo_id, ADDR_PRESENT_POSITION, 4, "Present_Position", alert
            )
            if reached is None:
                swing_ok = False
                break
            present_pos = _s32(reached)
            offset_from_start = present_pos - present
            # Against the target this reading belongs to, not against the start:
            # the start says only that the joint moved, the target says it
            # followed. See _report_travel.
            worst_error = max(worst_error, abs(present_pos - target))
            offsets.append(offset_from_start)
            degrees = offset_from_start / UNITS_PER_DEGREE
            print(
                f"Target: {target}, Present: {present_pos} (from start: {offset_from_start:+4d}, {degrees:+6.2f} deg)"
            )
            if alert.tripped:
                # The servo latched mid-swing (Overload on a leg that catches on
                # something is the case this robot actually sees). Stop
                # commanding it; the exit path below still parks and releases it.
                swing_ok = False
                break

        # Return to where the joint started, not to a fixed center.
        print("\nReturning to the starting position...")
        _write_register(
            packet_handler, port_handler, servo_id, ADDR_GOAL_POSITION, start_position, 4, "return to the start", alert
        )
        time.sleep(swing_period)

        final = _read_register(
            packet_handler, port_handler, servo_id, ADDR_PRESENT_POSITION, 4, "Present_Position", alert
        )
        if final is not None:
            print(f"Final position: {_s32(final)} (start: {present})")

        if _write_register(packet_handler, port_handler, servo_id, ADDR_TORQUE_ENABLE, 0, 1, "disable torque", alert):
            torque_enabled = False
            print("Torque disabled.")
        else:
            # Reported, not assumed: the operator decides whether to let go of
            # the robot based on this line.
            swing_ok = False
            print(f"WARNING: servo {servo_id} did not accept the torque release, so the joint may still be powered.")

        # Judged only on a swing that ran end to end. A run that broke off, or
        # one on a servo that latched mid-swing, has already been explained; a
        # second verdict about how well the joint followed would name a cause
        # that is not the one that stopped it.
        observed_span = max(offsets) - min(offsets) if offsets else 0
        moved = swing_ok and not alert.tripped and _report_travel(servo_id, commanded_peak, worst_error, observed_span)
        success = swing_ok and moved and not alert.tripped

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        # Return to the starting position and disable torque. Best effort: Ctrl+C
        # on a bus that is already failing must still end with torque off, and
        # the cleanup's own error must not replace the interrupt.
        try:
            if torque_enabled and start_position is not None:
                _write_register(
                    packet_handler,
                    port_handler,
                    servo_id,
                    ADDR_GOAL_POSITION,
                    start_position,
                    4,
                    "return to the start",
                    alert,
                )
                time.sleep(swing_period)
        except BaseException as exc:
            # BaseException, not Exception: a second Ctrl+C lands here as
            # KeyboardInterrupt, and anything that escapes this block would skip
            # the release below and leave the joint stiff.
            _print_safely(f"Warning: could not return servo {servo_id} to its starting position: {exc}")
        finally:
            if torque_enabled:
                _release_torque_quietly(packet_handler, port_handler, servo_id, alert)

    except Exception:
        # A dead or unplugged bus raises out of the vendor handler mid-swing. The
        # joint must not stay powered because of it, so release torque FIRST
        # (best effort — the bus may be gone), then say so: `print` itself raises
        # on a closed pipe or an unencodable character, and doing it first would
        # skip the release and replace the original error. Nothing is written
        # when this run never enabled torque, so a raise during the read
        # prologue cannot limp a joint the tool never touched.
        if torque_enabled:
            _release_torque_quietly(packet_handler, port_handler, servo_id, alert)
            _print_safely("\nUnexpected failure - torque released.")
        else:
            _print_safely("\nUnexpected failure - this run had not enabled torque, so nothing was released.")
        raise

    finally:
        # Nested so closePort() runs even if the restore writes raise on a dead
        # bus: an unclosed port keeps the next run from opening it, and it would
        # also mask whatever exception was already on its way out.
        #
        # One try per register, gain first. Sharing one meant a raise from either
        # restore skipped the other silently, and each warning names the register
        # it is about and the value left on the servo, because "profile/gain
        # registers" does not tell the operator what state the joint is in.
        try:
            try:
                if wrote_gain:
                    _restore_position_p_gain(
                        packet_handler, port_handler, servo_id, previous_gain, torque_enabled, alert
                    )
            except Exception as exc:
                print(
                    f"Warning: could not restore Position_P_Gain on servo {servo_id}, which is left at "
                    f"{SWING_POSITION_P_GAIN}, the SDK's own default: {exc}"
                )
            try:
                if wrote_profile:
                    _restore_profile(packet_handler, port_handler, servo_id, previous_profile, alert)
            except Exception as exc:
                print(
                    f"Warning: could not restore servo {servo_id}'s Profile_Acceleration/Profile_Velocity, which are "
                    f"left at {accel}/{velocity}: {exc}. Power-cycle the robot before running the SDK, or that joint "
                    "will move unlike its peers."
                )
        finally:
            port_handler.closePort()
            print(f"\nPort {port} closed.")

    return success


def _cmd_scan(args: argparse.Namespace) -> int:
    port_name = _resolve_port(args.port)
    if port_name is None:
        return 1

    found_ids = scan_dynamixel(port_name, args.baudrate, args.start, args.end)

    print("-" * 40)
    if found_ids:
        print(f"Found {len(found_ids)} DYNAMIXEL servo(s):")
        for dxl_id, model in found_ids:
            print(f"  ID: {dxl_id:3d} (Model: {model})")
    else:
        print("No DYNAMIXEL servos found")

    return 0 if found_ids else 1


def _cmd_power(args: argparse.Namespace) -> int:
    port_name = _resolve_port(args.port)
    if port_name is None:
        return 1
    bus = _open_bus(port_name, args.baudrate)
    if bus is None:
        return 1
    port, ph = bus

    print(f"reading {port_name} @ {args.baudrate}" + ("  (Ctrl+C to stop)" if args.watch else ""))
    answered = False
    try:
        while True:
            volts, curr, miss = _read_power(port, ph)
            if volts:
                answered = True
                vv = list(volts.values())
                lo, hi, mean = min(vv), max(vv), sum(vv) / len(vv)
                amps = sum(curr.values()) / 1000.0 if curr else 0.0
                line = f"V: min={lo:.1f} mean={mean:.2f} max={hi:.1f}  I~{amps:.2f}A  resp={len(vv)}/21"
                if miss:
                    line += f"  MISSING={miss}"
                print(line, flush=True)
                if args.per_id:
                    print("   " + "  ".join(f"{i}:{volts[i]:.1f}" for i in sorted(volts)), flush=True)
            else:
                print(f"no responses (bus down?)  MISSING={miss}", flush=True)
            if not args.watch:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        port.closePort()
    return 0 if answered else 1


def _cmd_errors(args: argparse.Namespace) -> int:
    port_name = _resolve_port(args.port)
    if port_name is None:
        return 1
    bus = _open_bus(port_name, args.baudrate)
    if bus is None:
        return 1
    port, ph = bus

    try:
        found = _read_error_status(port, ph)
        errored = _print_error_report(port_name, found)
        if not found:
            print(_DEAD_BUS_MESSAGE)
            return 1
        if errored:
            print(f"\n  servos with a latched hardware error: {errored}")
            print("  run the 'recover' subcommand to reboot them")
        return 0
    finally:
        port.closePort()


def _cmd_joints(args: argparse.Namespace) -> int:
    port_name = _resolve_port(args.port)
    if port_name is None:
        return 1
    bus = _open_bus(port_name, args.baudrate)
    if bus is None:
        return 1
    port, ph = bus

    try:
        found = _read_joint_states(port, ph)
        _print_joint_report(port_name, found)
        if not found:
            print(_DEAD_BUS_MESSAGE)
            return 1
        print("\n  read-only: nothing was moved and no torque was changed.")
        print(
            f"  pgain is Position_P_Gain: a servo reading far below its peers (default {SWING_POSITION_P_GAIN}) "
            "accepts goals without driving the joint."
        )
        return 0
    finally:
        port.closePort()


def _cmd_recover(args: argparse.Namespace) -> int:
    port_name = _resolve_port(args.port)
    if port_name is None:
        return 1
    bus = _open_bus(port_name, args.baudrate)
    if bus is None:
        return 1
    port, ph = bus

    try:
        found = _read_error_status(port, ph)
        errored = _print_error_report(port_name, found)
        if not found:
            print(_DEAD_BUS_MESSAGE)
            return 1
        if not errored:
            print("\n  nothing to reboot (no latched hardware errors).")
            return 0

        print(f"\n  rebooting {errored} ...")
        for i in errored:
            ph.reboot(port, i)
        time.sleep(REBOOT_SETTLE_SECONDS)

        after = _read_error_status(port, ph)
        still_errored: list[int] = []
        for i in errored:
            if i not in after:
                print(f"  id{i:2d}: NO-RESPONSE after reboot")
                still_errored.append(i)
                continue
            hw, tq, _ = after[i]
            hw_s = f"0x{hw:02x}({_err_names(hw)})" if hw is not None else "?"
            cleared = hw == 0
            if not cleared:
                still_errored.append(i)
            print(f"  id{i:2d}: hwerr={hw_s}  torque={tq}  -> {'cleared' if cleared else 'still errored'}")
        return 1 if still_errored else 0
    finally:
        port.closePort()


def _cmd_oscillate(args: argparse.Namespace) -> int:
    port_name = _resolve_port(args.port)
    if port_name is None:
        return 1

    if not oscillate_servo(args.id, port_name, args.baudrate, args.duration):
        print("\nTest failed!")
        return 1

    print("\nTest completed successfully!")
    return 0


def _servo_id(value: str) -> int:
    """Parse a `--id` argument, refusing anything the robot does not have.

    Unvalidated, this argument accepts 254 — the Dynamixel broadcast ID — which
    would address the whole bus and limp every joint at once.
    """
    try:
        servo_id = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"servo ID must be a whole number, got {value!r}") from None
    if servo_id not in IDS:
        raise argparse.ArgumentTypeError(f"servo ID must be between {IDS[0]} and {IDS[-1]}, got {servo_id}")
    return servo_id


def _duration(value: str) -> float:
    """Parse a `--duration` argument, refusing anything that cannot be swung.

    The swing count has a floor of two half-swings, so zero or a negative
    duration would still swing the joint twice after the user asked for none.
    Infinity and NaN reach `round()` and raise there instead.
    """
    try:
        duration = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"duration must be a number, got {value!r}") from None
    if not math.isfinite(duration) or duration <= 0:
        raise argparse.ArgumentTypeError(f"duration must be a positive number of seconds, got {value!r}")
    return duration


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--port",
        "-p",
        default=None,
        help="Serial port (e.g., /dev/ttyACM0, COM3); auto-detected when omitted",
    )
    common.add_argument(
        "--baudrate", "-b", type=int, default=DEFAULT_BAUDRATE, help=f"Baudrate (default: {DEFAULT_BAUDRATE})"
    )

    parser = argparse.ArgumentParser(description="Servo bus diagnostics for the Palmimo DevKit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", parents=[common], help="Ping an ID range and list responding servos")
    scan.add_argument(
        "--start", "-s", type=int, default=DEFAULT_ID_START, help=f"Start ID (default: {DEFAULT_ID_START})"
    )
    scan.add_argument("--end", "-e", type=int, default=DEFAULT_ID_END, help=f"End ID (default: {DEFAULT_ID_END})")
    scan.set_defaults(handler=_cmd_scan)

    power = subparsers.add_parser("power", parents=[common], help="Report every servo's input voltage and current")
    power.add_argument("--watch", action="store_true", help="Keep reading until interrupted instead of one snapshot")
    power.add_argument("--per-id", action="store_true", help="Print every servo's voltage")
    power.add_argument(
        "--interval", type=float, default=DEFAULT_INTERVAL, help=f"Seconds between reads (default: {DEFAULT_INTERVAL})"
    )
    power.set_defaults(handler=_cmd_power)

    errors = subparsers.add_parser("errors", parents=[common], help="Report hardware-error status and torque state")
    errors.set_defaults(handler=_cmd_errors)

    joints = subparsers.add_parser(
        "joints", parents=[common], help="Report every joint's present position, drive mode, torque, and temperature"
    )
    joints.set_defaults(handler=_cmd_joints)

    recover = subparsers.add_parser("recover", parents=[common], help="Reboot servos with a latched hardware error")
    recover.set_defaults(handler=_cmd_recover)

    oscillate = subparsers.add_parser(
        "oscillate", parents=[common], help="Swing one servo +/-15 degrees around its current position"
    )
    oscillate.add_argument("--id", type=_servo_id, required=True, help=f"Dynamixel servo ID ({IDS[0]}-{IDS[-1]})")
    oscillate.add_argument(
        "--duration",
        type=_duration,
        default=DEFAULT_DURATION,
        help=(
            "Seconds of swinging, rounded to a whole number of half-swings, plus one more "
            f"half-swing to return to the start (default: {DEFAULT_DURATION})"
        ),
    )
    oscillate.set_defaults(handler=_cmd_oscillate)

    return parser


def main() -> int:
    args = _build_parser().parse_args()
    exit_code: int = args.handler(args)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
