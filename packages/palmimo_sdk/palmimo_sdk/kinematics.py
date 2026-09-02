# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Pure leg inverse kinematics for the Palmimo hexapod.

Single source of truth for the IK math shared by the gait engine
(:mod:`palmimo_sdk.engine`) and any consumer that needs calibration poses.
Depends only on the standard library (``math``). All servo values are raw
Dynamixel ticks (0-4095, center = 2048); all distances are millimetres and all
angles degrees.
"""

import math
from collections.abc import Mapping


# Dynamixel resolution
NEUTRAL: int = 2048
TICK_PER_DEG: float = 4096.0 / 360.0
DEG_PER_TICK: float = 360.0 / 4096.0

# Link lengths in mm (from URDF)
L1: float = 32.0  # Coxa  (yaw axis to pitch1 axis)
L2: float = 45.3  # Femur (pitch1 axis to pitch2 axis)
L3: float = 100.0  # Tibia (pitch2 axis to foot tip)

# Mechanical pitch offsets — the joint angles when both servos sit at neutral
THETA2_OFFSET: float = -27.0  # degrees
THETA3_OFFSET: float = 0.0

# Leg mounting angle measured from +X axis, CCW positive (from URDF)
LEG_MOUNT_ANGLE: dict[int, float] = {
    1: 147.8,  # RL  (Rear Left)
    2: 90.0,  # ML  (Middle Left)
    3: 32.2,  # FL  (Front Left)
    4: -147.8,  # RR  (Rear Right)
    5: -90.0,  # MR  (Middle Right)
    6: -32.2,  # FR  (Front Right)
}

# Distance from the body origin to each leg's yaw axis (from the URDF), in mm.
LEG_MOUNT_RADIUS: dict[int, float] = {
    1: 72.1,
    2: 68.2,
    3: 72.1,
    4: 72.1,
    5: 68.2,
    6: 72.1,
}

# Left-side legs are mirrored, so their pitch servos turn the opposite way
LEFT_LEGS: tuple[int, ...] = (1, 2, 3)
RIGHT_LEGS: tuple[int, ...] = (4, 5, 6)


def base_foot_pos(leg_id: int) -> list[float]:
    """Neutral foot position relative to the leg yaw axis, body-aligned.

    The body's mount offset is intentionally excluded because the inverse
    kinematics operates on displacement from each leg's own yaw axis. Use
    :func:`leg_forward_kinematics` for an absolute body-frame position.
    """
    angle_rad = math.radians(LEG_MOUNT_ANGLE[leg_id])
    theta2_rad = math.radians(THETA2_OFFSET)
    theta3_rad = math.radians(THETA2_OFFSET + THETA3_OFFSET)

    l2_h = L2 * math.cos(theta2_rad)
    l3_h = L3 * math.cos(theta3_rad)
    total_reach = L1 + l2_h + l3_h

    x = total_reach * math.cos(angle_rad)
    y = total_reach * math.sin(angle_rad)
    z = L2 * math.sin(theta2_rad) + L3 * math.sin(theta3_rad)
    return [x, y, z]


def calc_2link_ik(x: float, z: float) -> tuple[float, float]:
    """2-link IK for the pitch1/pitch2 joints.

    ``x`` is the horizontal reach and ``z`` the vertical drop from the pitch1
    axis. Returns ``(pitch1_deg, pitch2_deg)`` relative to the neutral offsets.
    """
    l2_2 = L2 * L2
    l3_2 = L3 * L3
    xz2_sum = x * x + z * z
    sqrt_xz2 = math.sqrt(xz2_sum) if xz2_sum > 0 else 0.001

    ac1 = max(-1.0, min(1.0, (l2_2 - l3_2 + xz2_sum) / (2 * L2 * sqrt_xz2)))
    theta1 = math.atan2(z, x) + math.acos(ac1)

    ac2 = max(-1.0, min(1.0, (l2_2 + l3_2 - xz2_sum) / (2 * L2 * L3)))
    theta2 = -math.pi + math.acos(ac2)

    pitch1_diff = math.degrees(theta1) - THETA2_OFFSET
    pitch2_diff = math.degrees(theta2) - THETA3_OFFSET
    return pitch1_diff, pitch2_diff


def leg_ik(leg_id: int, dx: float, dy: float, dz: float) -> tuple[float, float, float]:
    """IK angles for a foot displacement ``(dx, dy, dz)`` in mm.

    Returns ``(yaw_deg, pitch1_deg, pitch2_deg)`` relative to neutral.
    """
    base = base_foot_pos(leg_id)
    tx = base[0] + dx
    ty = base[1] + dy
    tz = base[2] + dz

    n3_atan_deg = math.degrees(math.atan2(ty, tx))
    # The middle-left leg straddles the ±180° wrap; keep its yaw continuous.
    if leg_id == 2 and n3_atan_deg < 0:
        n3_atan_deg += 360

    yaw_diff = n3_atan_deg - LEG_MOUNT_ANGLE[leg_id]
    target_reach = math.sqrt(tx * tx + ty * ty) - L1
    pitch1_diff, pitch2_diff = calc_2link_ik(target_reach, tz)
    return yaw_diff, pitch1_diff, pitch2_diff


def leg_servo_ticks(leg_id: int, dx: float, dy: float, dz: float) -> dict[str, int]:
    """Raw servo ticks for one leg from a foot displacement via IK.

    Returns ``{"leg_<id>_yaw": ..., "leg_<id>_pitch1": ..., "leg_<id>_pitch2": ...}``.
    Left legs (1-3) have their pitch direction mirrored.
    """
    yaw_deg, pitch1_deg, pitch2_deg = leg_ik(leg_id, dx, dy, dz)
    pitch_sign = 1 if leg_id in LEFT_LEGS else -1
    return {
        f"leg_{leg_id}_yaw": NEUTRAL - int(yaw_deg * TICK_PER_DEG),
        f"leg_{leg_id}_pitch1": NEUTRAL + int(pitch1_deg * TICK_PER_DEG * pitch_sign),
        f"leg_{leg_id}_pitch2": NEUTRAL + int(pitch2_deg * TICK_PER_DEG * pitch_sign),
    }


def leg_forward_kinematics(leg_id: int, ticks: Mapping[str, int | float]) -> tuple[float, float, float]:
    """Return the foot position ``(x, y, z)`` in the body frame.

    Args:
        leg_id: Leg number from 1 through 6.
        ticks: Raw Dynamixel positions containing the leg's yaw, pitch1, and
            pitch2 keys.

    Returns:
        Absolute body-frame foot position in millimetres, including the offset
        from the body origin to the leg's yaw axis.
    """
    pitch_sign = 1 if leg_id in LEFT_LEGS else -1
    yaw_deg = (NEUTRAL - ticks[f"leg_{leg_id}_yaw"]) / TICK_PER_DEG
    pitch1_deg = (ticks[f"leg_{leg_id}_pitch1"] - NEUTRAL) / (TICK_PER_DEG * pitch_sign)
    pitch2_deg = (ticks[f"leg_{leg_id}_pitch2"] - NEUTRAL) / (TICK_PER_DEG * pitch_sign)

    theta1 = math.radians(THETA2_OFFSET + pitch1_deg)
    theta2 = math.radians(THETA3_OFFSET + pitch2_deg)
    reach = L1 + L2 * math.cos(theta1) + L3 * math.cos(theta1 + theta2)
    z = L2 * math.sin(theta1) + L3 * math.sin(theta1 + theta2)
    azimuth = math.radians(LEG_MOUNT_ANGLE[leg_id] + yaw_deg)
    mount_angle = math.radians(LEG_MOUNT_ANGLE[leg_id])
    mount_radius = LEG_MOUNT_RADIUS[leg_id]
    return (
        mount_radius * math.cos(mount_angle) + reach * math.cos(azimuth),
        mount_radius * math.sin(mount_angle) + reach * math.sin(azimuth),
        z,
    )


def rotation_matrix(roll: float, pitch: float, yaw: float) -> list[list[float]]:
    """3x3 body rotation matrix from roll/pitch/yaw (radians; X/Y/Z axes).

    Row-major, intrinsic ZYX (RPY) convention: ``R = Rz(yaw) @ Ry(pitch) @
    Rx(roll)``, so body-level motions compose consistently.
    """
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


def leg_servo_ticks_body(leg_id: int, body_pos: tuple[float, float, float], R: list[list[float]]) -> dict[str, int]:  # noqa: N803  (R = rotation matrix, math convention)
    """Servo ticks for one leg under a whole-body rotation ``R`` + translation ``body_pos``.

    The foot target is ``R @ (base_foot_pos + body_pos)`` (a rigid-body move of
    the whole robot), which the shared leg IK then turns into ticks. ``body_pos``
    is ``(x, y, z)`` mm; ``R`` a 3x3 row-major matrix (see :func:`rotation_matrix`).
    """
    base = base_foot_pos(leg_id)
    px, py, pz = base[0] + body_pos[0], base[1] + body_pos[1], base[2] + body_pos[2]
    tx = R[0][0] * px + R[0][1] * py + R[0][2] * pz
    ty = R[1][0] * px + R[1][1] * py + R[1][2] * pz
    tz = R[2][0] * px + R[2][1] * py + R[2][2] * pz
    return leg_servo_ticks(leg_id, tx - base[0], ty - base[1], tz - base[2])
