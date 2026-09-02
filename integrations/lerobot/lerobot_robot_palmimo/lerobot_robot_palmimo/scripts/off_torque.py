#!/usr/bin/env python3
"""Disable torque on Palmimo motors."""

import argparse
import sys

from lerobot.motors.dynamixel import DynamixelMotorsBus

from lerobot_robot_palmimo.motor_layout import build_palmimo_motors
from palmimo_sdk import SUPPORTED_MOTOR_MODELS, PortDetectionError, find_servo_port


def off_torque(port: str, motor_model: str) -> bool:
    """Disable torque on every responding motor.

    Returns:
        Whether torque was disabled successfully.
    """
    print(f"Port: {port}")
    print(f"Motor model: {motor_model}")
    print("=" * 50)

    motors = build_palmimo_motors(motor_model)
    print(f"Target: {len(motors)} motor(s)\n")

    # This operation uses raw motor IDs and does not require calibration.
    bus = DynamixelMotorsBus(port=port, motors=motors, calibration=None)

    try:
        # Open the port without the full MotorsBus handshake.
        bus.port_handler.openPort()
        bus.port_handler.setBaudRate(bus.default_baudrate)
        print(f"Baud rate: {bus.default_baudrate}")
        print("-" * 50)

        # Detect motors before writing so missing IDs do not block recovery.
        print("Scanning for motors...")
        found_motors = bus.broadcast_ping(num_retry=2)

        if found_motors is None:
            print("\nNG No motors found")
            print("   - Check that the power is on")
            print("   - Check the cable connection")
            return False

        found_ids = set(found_motors.keys())
        expected_ids = {m.id for m in motors.values()}

        print(f"Motors found: {len(found_motors)}")

        # Warn about expected motors that did not respond.
        missing_ids = expected_ids - found_ids
        if missing_ids:
            print(f"\n⚠️  Warning: {len(missing_ids)} motor(s) did not respond")
            for motor_name, motor in motors.items():
                if motor.id in missing_ids:
                    print(f"   - {motor_name} (ID: {motor.id})")
            print("\nDisabling torque only on the detected motors")

        # Limit the write to motors that responded.
        active_motors = {name: m for name, m in motors.items() if m.id in found_ids}

        if not active_motors:
            print("\nNG No operable motors found")
            return False

        print(f"\nOperating on: {len(active_motors)} motor(s)")
        print("-" * 50)

        # Disable torque only after discovery has established the target set.
        print("Disabling torque...")
        bus.disable_torque(list(active_motors.keys()))

        print("\nResult:")
        for motor_name in active_motors:
            print(f"  OK {motor_name}: torque off")

        print("-" * 50)
        print("OK Torque disabled on all motors")

        return True

    except Exception as e:
        print(f"\nNG Error: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        if bus.port_handler.is_open:
            bus.port_handler.closePort()


def main() -> None:
    parser = argparse.ArgumentParser(description="Palmimo torque disable")
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

    print("\nPalmimo torque disable\n")

    port = args.port
    if port is None:
        try:
            port = find_servo_port()
            print(f"Auto-detected port: {port}")
        except PortDetectionError as e:
            print(f"NG Port auto-detection failed: {e}")
            sys.exit(1)

    success = off_torque(port, args.motor_model)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
