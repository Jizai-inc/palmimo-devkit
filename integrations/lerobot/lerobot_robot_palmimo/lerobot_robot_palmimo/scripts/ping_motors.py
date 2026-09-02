#!/usr/bin/env python3
"""Check communication with Palmimo motors."""

import argparse
import sys

from lerobot.motors.dynamixel import DynamixelMotorsBus

from lerobot_robot_palmimo.motor_layout import build_palmimo_motors
from palmimo_sdk import SUPPORTED_MOTOR_MODELS, PortDetectionError, find_servo_port


def check_motors(port: str, motor_model: str) -> bool:
    """Check whether every expected motor responds.

    Returns:
        Whether every expected motor responded.
    """
    print(f"Port: {port}")
    print(f"Motor model: {motor_model}")
    print("=" * 50)

    motors = build_palmimo_motors(motor_model)
    print(f"Checking {len(motors)} motor(s)\n")

    # Discovery uses raw motor IDs and does not require calibration.
    bus = DynamixelMotorsBus(port=port, motors=motors, calibration=None)

    try:
        # Open the port without the full MotorsBus handshake.
        bus.port_handler.openPort()
        bus.port_handler.setBaudRate(bus.default_baudrate)
        print(f"Baud rate: {bus.default_baudrate}")
        print("-" * 50)

        # A broadcast ping discovers all motors currently on the bus.
        print("Scanning for motors...")
        found_motors = bus.broadcast_ping(num_retry=2)

        if found_motors is None:
            print("\nNG No motors found")
            print("   - Check that the power is on")
            print("   - Check the cable connection")
            return False

        print(f"\nMotors found: {len(found_motors)}")
        print("-" * 50)

        # Compare discovered IDs with the configured layout.
        expected_ids = {m.id for m in motors.values()}
        found_ids = set(found_motors.keys())

        # Report each expected motor separately for diagnosis.
        all_ok = True
        for name, motor in motors.items():
            if motor.id in found_ids:
                print(f"  OK {name} (ID: {motor.id})")
            else:
                print(f"  NG {name} (ID: {motor.id}) - no response")
                all_ok = False

        # Surface unknown IDs because they usually indicate a configuration error.
        unknown_ids = found_ids - expected_ids
        if unknown_ids:
            print("\n  Unrecognized motor IDs:")
            for uid in sorted(unknown_ids):
                print(f"    ID: {uid} (model: {found_motors[uid]})")

        print("-" * 50)
        if all_ok:
            print(f"OK All {len(motors)} motor(s) responded successfully")
        else:
            missing = len(expected_ids - found_ids)
            print(f"NG {missing} motor(s) did not respond")

        return all_ok

    except Exception as e:
        print(f"\nNG Error: {e}")
        return False

    finally:
        if bus.port_handler.is_open:
            bus.port_handler.closePort()


def main() -> None:
    parser = argparse.ArgumentParser(description="Palmimo motor connectivity check")
    parser.add_argument(
        "--port",
        type=str,
        default=None,
        help="Serial port (auto-detected if omitted)",
    )
    parser.add_argument(
        "--motor-model",
        type=str,
        default="xc330-m288",
        choices=SUPPORTED_MOTOR_MODELS,
        help="Dynamixel motor model name (default: xc330-m288)",
    )
    args = parser.parse_args()

    print("\nPalmimo motor connectivity check\n")

    port = args.port
    if port is None:
        try:
            port = find_servo_port()
            print(f"Auto-detected port: {port}")
        except PortDetectionError as e:
            print(f"NG Port auto-detection failed: {e}")
            sys.exit(1)

    success = check_motors(port, args.motor_model)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
