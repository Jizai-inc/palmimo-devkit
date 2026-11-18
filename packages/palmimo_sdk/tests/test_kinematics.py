"""Regression guard for palmimo_sdk.kinematics' pure IK/FK functions.

This is the single source of truth shared by engine and teleop, so a value
change shifts both their behavior. Pin representative values to catch
regressions introduced during extraction.
"""

import math

import pytest

from palmimo_sdk import kinematics


# Largest yaw change allowed between adjacent 0.25° roll steps. Real sweeps
# stay under ~80 ticks; a branch-cut flip is a near-full-turn (~4093 ticks),
# so the guard keeps a wide margin on both sides.
MAX_YAW_STEP = 150


def test_neutral_displacement_returns_neutral_ticks() -> None:
    """With zero displacement, every leg returns neutral (2048)."""
    for leg_id in range(1, 7):
        ticks = kinematics.leg_servo_ticks(leg_id, 0, 0, 0)
        assert ticks[f"leg_{leg_id}_yaw"] == kinematics.NEUTRAL
        assert ticks[f"leg_{leg_id}_pitch1"] == kinematics.NEUTRAL
        assert ticks[f"leg_{leg_id}_pitch2"] == kinematics.NEUTRAL


def test_leg_ik_zero_displacement_is_zero_angles() -> None:
    """With zero displacement, every IK angle is 0° (the neutral posture)."""
    for leg_id in range(1, 7):
        yaw, p1, p2 = kinematics.leg_ik(leg_id, 0, 0, 0)
        assert yaw == pytest.approx(0.0, abs=1e-6)
        assert p1 == pytest.approx(0.0, abs=1e-6)
        assert p2 == pytest.approx(0.0, abs=1e-6)


def test_pitch_sign_mirrors_on_left_legs() -> None:
    """Left legs (1-3) have the pitch direction flipped; right legs (4-6) are straightforward. Confirmed via lift (dz>0)."""
    left = kinematics.leg_servo_ticks(1, 0, 0, 40)
    right = kinematics.leg_servo_ticks(4, 0, 0, 40)
    # Left legs add a positive pitch offset, right legs subtract it.
    assert left["leg_1_pitch1"] > kinematics.NEUTRAL
    assert right["leg_4_pitch1"] < kinematics.NEUTRAL


def test_base_foot_pos_is_symmetric_left_right() -> None:
    """Left-right symmetric mounting legs have base foot positions whose y is symmetric with a sign flip."""
    # Leg 1 (RL, 147.8°) and leg 4 (RR, -147.8°) mirror across the X axis.
    b1 = kinematics.base_foot_pos(1)
    b4 = kinematics.base_foot_pos(4)
    assert b1[0] == pytest.approx(b4[0], abs=1e-9)
    assert b1[1] == pytest.approx(-b4[1], abs=1e-9)
    assert b1[2] == pytest.approx(b4[2], abs=1e-9)


def test_calc_2link_ik_reaches_known_value() -> None:
    """Pins a representative 2-link IK value (matches the pre-extraction formula)."""
    p1, p2 = kinematics.calc_2link_ik(120.0, 0.0)
    # Regression anchor — these are the values the validated engine produced.
    assert p1 == pytest.approx(80.597, abs=1e-3)
    assert p2 == pytest.approx(-74.980, abs=1e-3)


@pytest.mark.parametrize("leg_id", range(1, 7))
def test_leg_forward_kinematics_round_trips_small_lift(leg_id: int) -> None:
    """Feeding IK ticks back through FK recovers the original foot displacement within quantization error."""
    neutral = kinematics.leg_forward_kinematics(
        leg_id,
        kinematics.leg_servo_ticks(leg_id, 0.0, 0.0, 0.0),
    )
    lifted = kinematics.leg_forward_kinematics(
        leg_id,
        kinematics.leg_servo_ticks(leg_id, 0.0, 0.0, 10.0),
    )
    displacement = tuple(actual - base for actual, base in zip(lifted, neutral, strict=True))
    assert displacement == pytest.approx((0.0, 0.0, 10.0), abs=2.0)


def test_leg_forward_kinematics_includes_body_mount_offset() -> None:
    """FK coordinates aren't leg-local — they include the mount offset from the body origin."""
    leg_id = 3
    foot = kinematics.leg_forward_kinematics(
        leg_id,
        kinematics.leg_servo_ticks(leg_id, 0.0, 0.0, 0.0),
    )
    local = kinematics.base_foot_pos(leg_id)
    angle = kinematics.LEG_MOUNT_ANGLE[leg_id]
    assert foot[0] - local[0] == pytest.approx(kinematics.LEG_MOUNT_RADIUS[leg_id] * math.cos(math.radians(angle)))
    assert foot[1] - local[1] == pytest.approx(kinematics.LEG_MOUNT_RADIUS[leg_id] * math.sin(math.radians(angle)))
    assert foot[2] == pytest.approx(local[2])


def test_engine_and_kinematics_agree() -> None:
    """engine's _set_leg_from_ik writes the same ticks as kinematics."""
    from palmimo_sdk import MotionEngine

    engine = MotionEngine()
    # Cover both pitch_sign branches: leg 3 (left) and leg 4 (right).
    for leg_id in (3, 4):
        engine._set_leg_from_ik(leg_id, 30, 0, 40)
        expected = kinematics.leg_servo_ticks(leg_id, 30, 0, 40)
        for key, val in expected.items():
            assert engine._leg[key] == val


def test_wrap_deg_maps_to_half_open_interval() -> None:
    """Angles normalize into (-180, 180] — +180 is kept, -180 folds up to it."""
    assert kinematics.wrap_deg(180.0) == pytest.approx(180.0)
    assert kinematics.wrap_deg(-180.0) == pytest.approx(180.0)
    assert kinematics.wrap_deg(540.0) == pytest.approx(180.0)
    assert kinematics.wrap_deg(-179.9) == pytest.approx(-179.9)
    assert kinematics.wrap_deg(0.0) == pytest.approx(0.0)


@pytest.mark.parametrize("leg_id", range(1, 7))
def test_leg_yaw_stays_continuous_across_body_roll_sweep(leg_id: int) -> None:
    """Rolling the body from -60° to +60° never makes a leg's yaw jump.

    The rear legs (mount angle ±147.8°) sit next to atan2's ±180° branch cut, so
    an unnormalized yaw flips by nearly a full turn (~4093 ticks) mid-sweep.
    """
    previous = None
    for step in range(-240, 241):  # -60°..+60° in 0.25° increments
        roll = step * 0.25
        R = kinematics.rotation_matrix(math.radians(roll), 0.0, 0.0)
        tick = kinematics.leg_servo_ticks_body(leg_id, (0.0, 0.0, 0.0), R)[f"leg_{leg_id}_yaw"]
        if previous is not None:
            assert abs(tick - previous) < MAX_YAW_STEP, f"leg {leg_id} yaw jumped at roll={roll}°"
        previous = tick


def test_leg_2_yaw_matches_the_legacy_wrap_offset() -> None:
    """Leg 2's retired ``+360`` special case is subsumed by the general normalization.

    Exercised with lateral displacements large enough to drive leg 2's atan2
    negative, the only regime where that branch changed the result.
    """
    base = kinematics.base_foot_pos(2)
    for dx, dy in ((-10.0, -200.0), (-40.0, -250.0), (-5.0, -170.0), (-60.0, -300.0)):
        atan_deg = math.degrees(math.atan2(base[1] + dy, base[0] + dx))
        # Strictly inside the branch: a case landing exactly on the ±180° cut
        # would only agree with the legacy formula via ties-to-even rounding.
        assert atan_deg < -1e-6, f"dx={dx}, dy={dy} does not reach the legacy branch"
        legacy_yaw = (atan_deg + 360.0) - kinematics.LEG_MOUNT_ANGLE[2]
        assert kinematics.leg_ik(2, dx, dy, 0.0)[0] == pytest.approx(legacy_yaw, abs=1e-9)
