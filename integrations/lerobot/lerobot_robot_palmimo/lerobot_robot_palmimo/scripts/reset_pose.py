#!/usr/bin/env python3
"""Move Palmimo motors to their zero positions.

A recovery tool, not the robot path: it drives the bus directly, like its
sibling scripts here, so it still works when only part of the robot answers
(``--skip-missing``). The plugin itself never opens a bus — a normal bring-up
is ``Palmimo.connect()``, which runs the SDK's gain-ramped wake glide.
"""

import argparse
import sys
import time

from lerobot.motors.dynamixel import DynamixelMotorsBus, OperatingMode
from lerobot.motors.motors_bus import MotorsBus

from lerobot_robot_palmimo.motor_layout import build_palmimo_motors
from palmimo_sdk import SUPPORTED_MOTOR_MODELS, PortDetectionError, find_servo_port


# A joint carrying its own weight settles with a standing following error: the
# position loop stops correcting once its output balances gravity. Measured on
# hardware at the neutral stance, holding steady with no fault: legs 2-15 ticks,
# neck pitch 48-53 ticks. A move is therefore "reached" within this many ticks —
# a tighter bound reports a healthy robot as a failure.
POSITION_REACHED_TOLERANCE = 60

# XL330-M288 and XC330-M288 share a 4096-tick resolution and center at 2048.
ZERO_POSITION = 2048


def _filter_bus_motors(bus: MotorsBus, found_ids: set[int]) -> tuple[int, int]:
    """Shrink bus.motors to only those whose ID is in found_ids.

    Rebuilds the derived id→model/name maps and invalidates the cached ids/models
    properties so subsequent sync_read / sync_write only touch responding motors.
    Returns (kept, skipped).
    """
    original = len(bus.motors)
    bus.motors = {name: m for name, m in bus.motors.items() if m.id in found_ids}
    bus._id_to_model_dict = {m.id: m.model for m in bus.motors.values()}
    bus._id_to_name_dict = {m.id: name for name, m in bus.motors.items()}
    for cached in ("ids", "models", "_has_different_ctrl_tables"):
        bus.__dict__.pop(cached, None)
    return len(bus.motors), original - len(bus.motors)


def reset_initial_pose(bus: MotorsBus, velocity: int = 300, timeout: float = 15.0) -> bool:
    """Move every configured joint to its zero position.

    Args:
        bus: Connected motor bus, already restricted to the motors to move.
        velocity: Dynamixel Profile Velocity value.
        timeout: Seconds to wait for the move to complete.

    Returns:
        Whether every motor reached the zero position.
    """
    # Operating mode is EEPROM-backed and must be changed with torque disabled.
    with bus.torque_disabled():
        for motor in bus.motors:
            bus.write("Operating_Mode", motor, OperatingMode.POSITION.value)

    # Limit velocity so the motors ease into the zero position instead of snapping there.
    bus.sync_write("Profile_Velocity", velocity, normalize=False)

    # Re-enable torque only after configuring the motion profile.
    bus.enable_torque()
    time.sleep(0.1)

    bus.sync_write("Goal_Position", dict.fromkeys(bus.motors, ZERO_POSITION), normalize=False)

    # Poll until every motor is within the acceptable error.
    start_time = time.time()
    print("\nMoving: ", end="", flush=True)
    last_dot_time = start_time

    # Let the USB-Dynamixel bridge drain stale data before the first read.
    time.sleep(0.2)

    while time.time() - start_time < timeout:
        bus.port_handler.clearPort()
        current_positions = bus.sync_read("Present_Position", normalize=False, num_retry=3)

        all_reached = all(abs(pos - ZERO_POSITION) < POSITION_REACHED_TOLERANCE for pos in current_positions.values())

        if all_reached:
            print(" Done!")
            return True

        # Keep long moves visibly active without flooding the terminal.
        if time.time() - last_dot_time >= 0.5:
            print(".", end="", flush=True)
            last_dot_time = time.time()

        time.sleep(0.1)

    print(" Timeout")
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Palmimo zero-position reset")
    parser.add_argument(
        "--port",
        type=str,
        default=None,
        help="Serial port (auto-detected if omitted)",
    )
    parser.add_argument(
        "--velocity",
        type=int,
        default=300,
        help="Profile Velocity value (default: 300)",
    )
    parser.add_argument(
        "--skip-missing",
        action="store_true",
        help="Skip unresponsive motors and operate only on the ones detected",
    )
    parser.add_argument(
        "--motor-model",
        type=str,
        default="xc330-m288",
        choices=SUPPORTED_MOTOR_MODELS,
        help="Dynamixel motor model name (default: xc330-m288)",
    )
    args = parser.parse_args()

    print("\nPalmimo zero-position reset\n")

    port = args.port
    if port is None:
        try:
            port = find_servo_port()
            print(f"Auto-detected port: {port}")
        except PortDetectionError as e:
            print(f"NG Port auto-detection failed: {e}")
            sys.exit(1)

    print(f"Port: {port}")
    print(f"Motor model: {args.motor_model}")
    print(f"Velocity: {args.velocity}")
    if args.skip_missing:
        print("Mode: skip-missing (operating only on detected motors)")
    print("=" * 50)

    # The shared layout, so this script and the plugin address the same motors.
    bus = DynamixelMotorsBus(port=port, motors=build_palmimo_motors(args.motor_model), calibration=None)

    try:
        if args.skip_missing:
            # Probe without a handshake, then restrict the bus to responding IDs
            # so recovery can proceed when only part of the robot is reachable.
            print("Opening port and checking connectivity...")
            bus.connect(handshake=False)
            found_map = bus.broadcast_ping(num_retry=3) or {}
            kept, skipped = _filter_bus_motors(bus, set(found_map.keys()))
            print(f"Detected: {kept} / Skipped: {skipped}")
            if kept == 0:
                print("NG No operable motors found")
                sys.exit(1)
        else:
            print("Connecting to the robot...")
            bus.connect()
        print(f"Connected: {len(bus.motors)} motor(s)")
        print("-" * 50)

        # Move the reachable motors to zero.
        print("Moving to zero position...")
        success = reset_initial_pose(bus, velocity=args.velocity, timeout=5)

        if success:
            print("-" * 50)
            print("OK Zero-position reset complete")
        else:
            print("-" * 50)
            print("⚠️  Some motors may not have reached the target position")

        # Report final positions so partial failures are actionable.
        print("\nFinal positions:")
        bus.port_handler.clearPort()
        final_positions = bus.sync_read("Present_Position", normalize=False, num_retry=3)

        for motor_name, pos in final_positions.items():
            diff = abs(pos - ZERO_POSITION)
            status = "OK" if diff < POSITION_REACHED_TOLERANCE else "NG"
            print(f"  {status} {motor_name}: {pos} (diff: {diff})")

        sys.exit(0 if success else 1)

    except Exception as e:
        print(f"\nNG Error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

    finally:
        if bus.is_connected:
            # Keep torque enabled so the robot continues holding the reset pose.
            bus.disconnect(disable_torque=False)


if __name__ == "__main__":
    main()
