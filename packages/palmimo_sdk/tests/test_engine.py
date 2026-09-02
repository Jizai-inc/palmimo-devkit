"""MotionEngine — pure computation, no I/O. Runs without any hardware deps."""

import itertools

import pytest

from palmimo_sdk import Motion, MotionEngine


# Servo safe range (AGENTS.md): keep every commanded tick well inside this.
SAFE_LO, SAFE_HI = 200, 3900

ALL_MOTIONS = [
    Motion.FORWARD,
    Motion.BACKWARD,
    Motion.STRAFE_LEFT,
    Motion.STRAFE_RIGHT,
    Motion.ROTATE_LEFT,
    Motion.ROTATE_RIGHT,
    Motion.DANCE,
    Motion.BODY_TILT,
    Motion.PUSHUP,
    Motion.WAVE,
    Motion.WAVE_BOTH,
    Motion.CLAP,
    Motion.CREEP,
    Motion.NOD,
    Motion.HEAD_SHAKE,
    Motion.BOW,
    Motion.STRETCH,
]


def _run_seconds(engine: MotionEngine, seconds: float) -> dict[str, int]:
    """Step the engine for seconds worth of time at 60fps and return the final positions."""
    pos = engine.get_positions()
    for _ in range(round(seconds * 60)):
        pos = engine.step()
    return pos


def test_step_emits_20_motors() -> None:
    """step() returns 18 leg axes + 2 neck axes = 20 motors."""
    engine = MotionEngine()
    pos = engine.step()
    assert len(pos) == 20
    assert {"leg_1_yaw", "leg_6_pitch2", "neck_yaw", "neck_pitch1"} <= set(pos)


@pytest.mark.parametrize("motion", ALL_MOTIONS, ids=lambda m: m.name.lower())
def test_every_motion_stays_in_safe_range(motion: Motion) -> None:
    """Every motion stays within the safe tick range even after 120 steps."""
    engine = MotionEngine()
    engine.motion = motion
    for _ in range(120):
        pos = engine.step()
        for name, tick in pos.items():
            assert SAFE_LO <= tick <= SAFE_HI, f"{motion.name}/{name}={tick} out of range"


def test_step_is_deterministic() -> None:
    """Same initial conditions and same input produce identical output (pure computation)."""
    a, b = MotionEngine(), MotionEngine()
    a.motion = b.motion = Motion.FORWARD
    for _ in range(60):
        assert a.step() == b.step()


def test_reset_returns_to_neutral() -> None:
    """reset() returns every servo to neutral (2048) and sets motion to IDLE."""
    engine = MotionEngine()
    engine.motion = Motion.DANCE
    for _ in range(30):
        engine.step()
    engine.reset()
    assert engine.motion is Motion.IDLE
    assert all(tick == engine.NEUTRAL for tick in engine.get_positions().values())


def test_get_positions_does_not_advance() -> None:
    """get_positions() doesn't advance the phase (unlike step(), it has no side effects)."""
    engine = MotionEngine()
    engine.motion = Motion.FORWARD
    engine.step()
    snapshot = engine.get_positions()
    assert engine.get_positions() == snapshot


def _swing_leg_pose(step_height: float) -> dict[str, int]:
    """Take swing leg leg_1's pose after 5 steps of walking at the given step_height.

    "swing phase after 5 steps" depends on the gait_speed default (at the
    default 0.012, phase≈0.38rad, so leg_1 in TRIPOD_A is in swing phase where
    sin>0). Revisit this if the default changes.
    """
    engine = MotionEngine(step_height=step_height)
    engine.motion = Motion.FORWARD
    for _ in range(5):
        positions = engine.step()
    return positions


def test_step_height_drives_swing_lift_without_touching_stride() -> None:
    """step_height governs the swing leg's lift height without affecting the yaw stride."""
    low, high = _swing_leg_pose(20.0), _swing_leg_pose(45.0)
    neutral = MotionEngine.NEUTRAL

    assert abs(high["leg_1_pitch1"] - neutral) > abs(low["leg_1_pitch1"] - neutral)
    assert abs(high["leg_1_pitch2"] - neutral) > abs(low["leg_1_pitch2"] - neutral)
    assert high["leg_1_yaw"] == low["leg_1_yaw"]


def test_dance_defaults_match_official_feel() -> None:
    """dance's defaults are hardware-tuned values (roll5/pivot125/arc/dwell0.2/speed0.017)."""
    e = MotionEngine()
    assert e.dance_roll_deg == 5.0
    assert e.dance_pivot_h == 125.0
    assert e.dance_dwell == 0.2
    assert e.dance_speed == 0.0149  # fps-compensated so production matches the tuned feel
    assert e.dance_level_head is False  # arc head, not leveled


def test_dance_advances_by_dance_speed_not_gait_speed() -> None:
    """dance's tempo is governed by dance_speed, independent of walking's gait_speed."""
    e = MotionEngine(gait_speed=0.5)  # walk speed deliberately very different
    e.dance_speed = 0.01
    e.motion = Motion.DANCE  # motion setter resets gait phase to 0
    e.step()
    assert e.gait_phase == pytest.approx(0.01 * 0.8)


def test_selecting_dance_resets_gait_phase() -> None:
    """Selecting DANCE resets the phase to 0, so the sway always starts centered."""
    e = MotionEngine()
    e.motion = Motion.FORWARD
    for _ in range(10):
        e.step()
    assert e.gait_phase != 0.0
    e.motion = Motion.DANCE
    assert e.gait_phase == 0.0


def test_neck_target_is_clamped() -> None:
    """set_neck clamps input beyond the normalized range [-1, 1]."""
    engine = MotionEngine()
    engine.set_neck(pitch=5.0, yaw=-5.0)
    for _ in range(60):
        engine.step()
    pos = engine.get_positions()
    # amplitude stays within 300 ticks (neutral 2048 ± 300)
    assert abs(pos["neck_pitch1"] - engine.NEUTRAL) <= 300
    assert abs(pos["neck_yaw"] - engine.NEUTRAL) <= 300


def test_neck_travel_deg_is_public_per_axis_and_matches_amplitude_ticks() -> None:
    """NECK_PITCH_TRAVEL_DEG / NECK_YAW_TRAVEL_DEG are public per-axis constants
    derived from NECK_AMPLITUDE_TICKS and TICK_PER_DEG.

    This is the public API that palmimo_sdk.robot.Palmimo.look()'s
    NeckPitchDegrees/NeckYawDegrees conversion and LookTool's schema range
    reference (the constants were split per axis because pitch/yaw range of
    motion may diverge in the future; today they share the same
    _neck_amplitude in the engine implementation, so the values match).
    """
    expected = MotionEngine.NECK_AMPLITUDE_TICKS / MotionEngine.TICK_PER_DEG
    assert pytest.approx(expected) == MotionEngine.NECK_PITCH_TRAVEL_DEG
    assert pytest.approx(expected) == MotionEngine.NECK_YAW_TRAVEL_DEG
    # measured: 300 ticks / (4096/360) ≈ 26.37 degrees.
    assert pytest.approx(26.3671875) == MotionEngine.NECK_PITCH_TRAVEL_DEG
    assert pytest.approx(26.3671875) == MotionEngine.NECK_YAW_TRAVEL_DEG


def test_gait_phase_is_readonly_and_advances() -> None:
    """gait_phase is a read-only property that advances from 0 on every step()."""
    engine = MotionEngine()
    assert engine.gait_phase == 0.0
    engine.motion = Motion.FORWARD
    engine.step()
    assert engine.gait_phase > 0.0
    with pytest.raises(AttributeError):
        engine.gait_phase = 0.5  # type: ignore[misc]  # deliberately verifying the read-only contract


def test_set_leg_positions_seeds_only_leg_keys() -> None:
    """set_leg_positions writes only leg_* keys to internal state and ignores non-leg keys."""
    engine = MotionEngine()
    pose = {f"leg_{i}_yaw": 2200 for i in range(1, 7)}
    pose["neck_yaw"] = 9999
    engine.set_leg_positions(pose)
    snap = engine.get_positions()
    assert snap["leg_1_yaw"] == 2200
    assert snap["neck_yaw"] == engine.NEUTRAL


# ----------------------------------------------------------------
# BOW (bowing) — a one-shot posture: enter → hold → exit
# ----------------------------------------------------------------


def _bow_total_s(engine: MotionEngine) -> float:
    return engine.bow_enter_s + engine.bow_hold_s + engine.bow_exit_s


def test_bow_default_duration_fits_greeting() -> None:
    """The default BOW's total length fits a 1.5-2 second greeting."""
    assert 1.5 <= _bow_total_s(MotionEngine()) <= 2.0


def test_bow_stays_in_safe_range_full_length() -> None:
    """Every tick stays in the safe range even running BOW through its full length plus lingering hold."""
    engine = MotionEngine()
    engine.motion = Motion.BOW
    for _ in range(round((_bow_total_s(engine) + 1.0) * 60)):
        pos = engine.step()
        for name, tick in pos.items():
            assert SAFE_LO <= tick <= SAFE_HI, f"BOW/{name}={tick} out of range"


def test_bow_completes_and_holds_neutral() -> None:
    """BOW plays only once; after it finishes the legs settle at neutral and the neck at rest center."""
    engine = MotionEngine()
    engine.motion = Motion.BOW
    _run_seconds(engine, _bow_total_s(engine) + 1.0)
    settled = engine.get_positions()
    # neck pitch settles at rest-trim center (facing-forward home); everything else goes to raw neutral.
    assert settled["neck_pitch1"] == engine.neck_pitch_center()
    assert all(abs(t - engine.NEUTRAL) <= 2 for k, t in settled.items() if k != "neck_pitch1")
    # one-shot: stepping further doesn't start a second cycle
    assert _run_seconds(engine, 0.5) == settled


def test_bow_pose_tilts_forward_on_planted_rear() -> None:
    """During the hold, the body dips along a front-1.0/mid-0.5/rear-0.0 gradient, with the rear leg staying at its grounded hinge."""
    engine = MotionEngine()
    engine.motion = Motion.BOW
    mid = _run_seconds(engine, engine.bow_enter_s + engine.bow_hold_s / 2)
    n = engine.NEUTRAL
    front = abs(mid["leg_3_pitch1"] - n)  # FL: dips the most
    middle = abs(mid["leg_2_pitch1"] - n)  # ML: dips half as much
    assert front > middle > 0
    assert mid["leg_1_pitch1"] == n  # RL: rear leg stays at neutral (doesn't lift)
    assert mid["neck_pitch1"] > n  # neck lowers (positive offset = downward on hardware)


def test_bow_pose_left_right_symmetric() -> None:
    """The hold posture is left-right symmetric (same angle apart from the pitch_sign flip)."""
    engine = MotionEngine()
    engine.motion = Motion.BOW
    mid = _run_seconds(engine, engine.bow_enter_s + engine.bow_hold_s / 2)
    n = engine.NEUTRAL
    for left, right in ((3, 6), (2, 5), (1, 4)):
        for joint in ("pitch1", "pitch2"):
            lo = mid[f"leg_{left}_{joint}"] - n
            ro = mid[f"leg_{right}_{joint}"] - n
            assert abs(lo + ro) <= 1, f"{left}/{right} {joint}: {lo} vs {ro}"
        assert mid[f"leg_{left}_yaw"] == mid[f"leg_{right}_yaw"] == n


def test_bow_hold_is_still() -> None:
    """During the hold interval, leg ticks are completely still (no swaying)."""
    engine = MotionEngine()
    engine.motion = Motion.BOW
    _run_seconds(engine, engine.bow_enter_s + 0.05)
    reference = {k: v for k, v in engine.step().items() if k.startswith("leg_")}
    for _ in range(round((engine.bow_hold_s - 0.15) * 60)):
        frame = engine.step()
        assert {k: v for k, v in frame.items() if k.startswith("leg_")} == reference


def test_bow_interrupted_by_idle_recovers() -> None:
    """Switching to IDLE mid-hold still returns legs to neutral and the neck to rest center."""
    engine = MotionEngine()
    engine.motion = Motion.BOW
    _run_seconds(engine, engine.bow_enter_s + engine.bow_hold_s / 2)
    engine.motion = Motion.IDLE
    _run_seconds(engine, 2.0)
    pos = engine.get_positions()
    assert pos["neck_pitch1"] == engine.neck_pitch_center()
    assert all(t == engine.NEUTRAL for k, t in pos.items() if k != "neck_pitch1")


def test_bow_retrigger_replays_from_start() -> None:
    """Re-setting BOW after completion replays it from the start."""
    engine = MotionEngine()
    engine.motion = Motion.BOW
    _run_seconds(engine, _bow_total_s(engine) + 0.5)
    engine.motion = Motion.BOW
    mid = _run_seconds(engine, engine.bow_enter_s + engine.bow_hold_s / 2)
    assert abs(mid["leg_3_pitch1"] - engine.NEUTRAL) > 100  # the chest dips again


# ----------------------------------------------------------------
# STRETCH (a stretch / rise) — feet stay grounded throughout: wind-up → rise → hold → descend
# ----------------------------------------------------------------


def _stretch_total_s(engine: MotionEngine) -> float:
    return engine.stretch_duration_s


def _stretch_mid_hold_s(engine: MotionEngine) -> float:
    return engine.stretch_settle_s + engine.stretch_rise_s + engine.stretch_hold_s / 2


def test_stretch_default_duration() -> None:
    """The default STRETCH's total length is 2-4 seconds (wind-up 0.2 + rise 0.8 + hold 1.0 + descend 0.8)."""
    assert 2.0 <= _stretch_total_s(MotionEngine()) <= 4.0


def test_stretch_stays_in_safe_range_full_length() -> None:
    """Every tick stays in the safe range even running STRETCH through its full length plus lingering hold."""
    engine = MotionEngine()
    engine.motion = Motion.STRETCH
    for _ in range(round((_stretch_total_s(engine) + 1.0) * 60)):
        pos = engine.step()
        for name, tick in pos.items():
            assert SAFE_LO <= tick <= SAFE_HI, f"STRETCH/{name}={tick} out of range"


def test_stretch_completes_and_holds_neutral() -> None:
    """STRETCH plays only once; after it finishes the legs settle at neutral and the neck at rest center."""
    engine = MotionEngine()
    engine.motion = Motion.STRETCH
    _run_seconds(engine, _stretch_total_s(engine) + 1.0)
    settled = engine.get_positions()
    assert settled["neck_pitch1"] == engine.neck_pitch_center()
    assert all(abs(t - engine.NEUTRAL) <= 2 for k, t in settled.items() if k != "neck_pitch1")
    assert _run_seconds(engine, 0.5) == settled


def test_stretch_winds_up_before_rising() -> None:
    """Before rising, it dips in a wind-up (the femur rises = crouch direction)."""
    engine = MotionEngine()
    engine.motion = Motion.STRETCH
    dip = _run_seconds(engine, engine.stretch_settle_s)
    n = engine.NEUTRAL
    assert dip["leg_1_pitch1"] > n  # left leg: femur rises = crouching
    assert dip["leg_4_pitch1"] < n  # right leg mirrors it


def test_stretch_tall_pose_folds_all_femurs() -> None:
    """During the hold, every leg's femur folds downward (sign-mirrored left/right) and the neck rises."""
    engine = MotionEngine()
    engine.motion = Motion.STRETCH
    mid = _run_seconds(engine, _stretch_mid_hold_s(engine))
    n = engine.NEUTRAL
    for left in (1, 2, 3):
        assert mid[f"leg_{left}_pitch1"] < n  # left leg: femur lowers = pitch1 negative
        assert mid[f"leg_{left}_pitch2"] > n  # knee opens at a measured ratio of 0.82
    for right in (4, 5, 6):
        assert mid[f"leg_{right}_pitch1"] > n
        assert mid[f"leg_{right}_pitch2"] < n
    assert mid["neck_pitch1"] < n  # neck points up (negative offset = up on hardware)


def test_stretch_pose_left_right_symmetric() -> None:
    """The hold posture is left-right symmetric (same angle apart from the pitch_sign flip)."""
    engine = MotionEngine()
    engine.motion = Motion.STRETCH
    mid = _run_seconds(engine, _stretch_mid_hold_s(engine))
    n = engine.NEUTRAL
    for left, right in ((3, 6), (2, 5), (1, 4)):
        for joint in ("yaw", "pitch1", "pitch2"):
            lo = mid[f"leg_{left}_{joint}"] - n
            ro = mid[f"leg_{right}_{joint}"] - n
            assert abs(lo + ro) <= 1, f"{left}/{right} {joint}: {lo} vs {ro}"


def test_stretch_hold_is_still() -> None:
    """No swaying during the 1-second hold either (leg ticks are completely still)."""
    engine = MotionEngine()
    engine.motion = Motion.STRETCH
    _run_seconds(engine, engine.stretch_settle_s + engine.stretch_rise_s + 0.05)
    reference = {k: v for k, v in engine.step().items() if k.startswith("leg_")}
    for _ in range(round((engine.stretch_hold_s - 0.15) * 60)):
        frame = engine.step()
        assert {k: v for k, v in frame.items() if k.startswith("leg_")} == reference


def test_stretch_interrupted_by_idle_recovers() -> None:
    """Switching to IDLE mid-hold still returns legs to neutral and the neck to rest center."""
    engine = MotionEngine()
    engine.motion = Motion.STRETCH
    _run_seconds(engine, _stretch_mid_hold_s(engine))
    engine.motion = Motion.IDLE
    _run_seconds(engine, 2.0)
    pos = engine.get_positions()
    assert pos["neck_pitch1"] == engine.neck_pitch_center()
    assert all(t == engine.NEUTRAL for k, t in pos.items() if k != "neck_pitch1")


def test_posture_retrigger_mid_gesture_is_ignored() -> None:
    """A reselect mid-playback is ignored (prevents a single-frame snap under load)."""
    cases = (
        (Motion.BOW, lambda e: e.bow_enter_s + e.bow_hold_s / 2),
        (Motion.STRETCH, _stretch_mid_hold_s),
    )
    for motion, mid_fn in cases:
        engine = MotionEngine()
        engine.motion = motion
        mid = _run_seconds(engine, mid_fn(engine))
        engine.motion = motion  # reselect mid-playback
        after = engine.step()
        for key in after:  # not replaying from the start = no snap
            assert abs(after[key] - mid[key]) <= 20, (motion, key)


def test_look_works_after_posture_one_shot() -> None:
    """After the one-shot completes, look() reclaims ownership of the neck."""
    for motion, total_fn in ((Motion.BOW, lambda e: e.bow_duration_s), (Motion.STRETCH, _stretch_total_s)):
        engine = MotionEngine()
        engine.motion = motion
        _run_seconds(engine, total_fn(engine) + 0.5)
        engine.set_neck(yaw=0.8)  # equivalent to look() after completion
        _run_seconds(engine, 1.0)
        pos = engine.get_positions()
        assert pos["neck_yaw"] > engine.NEUTRAL + 200, motion


def test_stretch_retrigger_replays_from_start() -> None:
    """Re-setting STRETCH after completion replays it from the start."""
    engine = MotionEngine()
    engine.motion = Motion.STRETCH
    _run_seconds(engine, _stretch_total_s(engine) + 0.5)
    engine.motion = Motion.STRETCH
    mid = _run_seconds(engine, _stretch_mid_hold_s(engine))
    assert abs(mid["leg_3_pitch1"] - engine.NEUTRAL) > 100  # the femur folds again


# ----------------------------------------------------------------
# Control rate (dt) — posture one-shot second-knobs match wall-clock time even at non-60fps
# ----------------------------------------------------------------


def test_engine_rejects_non_positive_dt() -> None:
    """dt only accepts a positive number of seconds."""
    with pytest.raises(ValueError, match="dt"):
        MotionEngine(dt=0.0)


def test_posture_phase_scales_with_dt() -> None:
    """With dt=1/30, the engine completes BOW/STRETCH using a step count scaled for 30fps."""
    for motion, total_fn in ((Motion.BOW, _bow_total_s), (Motion.STRETCH, _stretch_total_s)):
        engine = MotionEngine(dt=1.0 / 30.0)
        engine.motion = motion
        for _ in range(round((total_fn(engine) + 1.0) * 30)):
            engine.step()
        settled = engine.get_positions()
        assert settled["neck_pitch1"] == engine.neck_pitch_center(), motion
        assert all(abs(t - engine.NEUTRAL) <= 2 for k, t in settled.items() if k != "neck_pitch1"), motion


def test_posture_hold_pose_is_rate_independent() -> None:
    """The mid-hold posture (including the neck) is identical regardless of control rate (measured on a wall-clock basis)."""
    poses = []
    for fps in (60, 30):
        engine = MotionEngine(dt=1.0 / fps)
        engine.motion = Motion.BOW
        for _ in range(round((engine.bow_enter_s + engine.bow_hold_s / 2) * fps)):
            engine.step()
        poses.append(engine.get_positions())
    assert poses[0] == poses[1]


# ================================================================
# Neck gestures (NOD / HEAD_SHAKE)
# ================================================================

# Duration at default knobs: NOD ≈ (2 + (1.6-1)/2) cycles x 0.65s ≈ 1.5s (90 steps);
# SHAKE = 4 swings x 0.66s/2 ≈ 1.32s (80 steps). Run 150 steps with margin.
GESTURE_STEPS = 150


def test_nod_moves_only_neck_pitch() -> None:
    """NOD only moves neck_pitch1. The 18 leg axes and neck_yaw stay at neutral."""
    engine = MotionEngine()
    engine.motion = Motion.NOD
    frames = [engine.step() for _ in range(GESTURE_STEPS)]
    assert any(f["neck_pitch1"] != engine.NEUTRAL for f in frames)
    for f in frames:
        assert f["neck_yaw"] == engine.NEUTRAL
        for leg_id in range(1, 7):
            for suffix in ("yaw", "pitch1", "pitch2"):
                assert f[f"leg_{leg_id}_{suffix}"] == engine.NEUTRAL


def test_head_shake_moves_only_neck_yaw() -> None:
    """HEAD_SHAKE only moves neck_yaw. The 18 leg axes and neck_pitch1 (rest position) stay put."""
    engine = MotionEngine()
    for _ in range(60):  # let the neck settle at the rest-trim position before firing
        engine.step()
    engine.motion = Motion.HEAD_SHAKE
    frames = [engine.step() for _ in range(GESTURE_STEPS)]
    assert any(f["neck_yaw"] != engine.NEUTRAL for f in frames)
    rest = engine.neck_pitch_center()
    for f in frames:
        assert f["neck_pitch1"] == rest
        for leg_id in range(1, 7):
            for suffix in ("yaw", "pitch1", "pitch2"):
                assert f[f"leg_{leg_id}_{suffix}"] == engine.NEUTRAL


def test_nod_first_stroke_is_down_and_decays() -> None:
    """NOD's first stroke is downward, and the second stroke decays to a shallower dip.

    On hardware, a positive pitch offset means chin-down (direction confirmed
    on hardware; this is the opposite of set_neck's "positive = up" docstring).
    """
    engine = MotionEngine()
    for _ in range(60):  # let the neck settle at the rest-trim position before firing
        engine.step()
    engine.motion = Motion.NOD
    rest = engine.neck_pitch_center()
    offsets = [engine.step()["neck_pitch1"] - rest for _ in range(GESTURE_STEPS)]
    first_moving = next(o for o in offsets if o != 0)
    assert first_moving > 0  # chin down first (positive = down on hardware)
    # compare the peak depth of cycle 1 vs cycle 2 (window derived from the knobs; cycle 2 is shallower by the decay factor)
    cycle1_end = round((engine.nod_period / 2 * engine.nod_first_down_scale + engine.nod_period / 2) * 60)
    cycle2_end = cycle1_end + round(engine.nod_period * 60)
    dip1 = max(offsets[:cycle1_end])
    dip2 = max(offsets[cycle1_end:cycle2_end])
    amp = engine.nod_amp_deg * engine.TICK_PER_DEG
    assert dip1 == pytest.approx(amp, abs=3)
    assert dip2 == pytest.approx(amp * engine.nod_decay, abs=3)


def test_head_shake_is_symmetric_and_alternates() -> None:
    """SHAKE swings ± (left-right symmetric), crossing neutral alternately and decaying over the swing count."""
    engine = MotionEngine()
    engine.motion = Motion.HEAD_SHAKE
    offsets = [engine.step()["neck_yaw"] - engine.NEUTRAL for _ in range(GESTURE_STEPS)]
    amp = engine.shake_amp_deg * engine.TICK_PER_DEG
    assert max(offsets) == pytest.approx(amp, abs=3)  # swing 1: full amplitude on the + side
    assert min(offsets) == pytest.approx(-amp * engine.shake_decay, abs=3)  # swing 2: - side
    # number of sign flips between adjacent peaks = swing count - 1
    signs = [1 if o > 0 else -1 for o in offsets if o != 0]
    changes = sum(1 for a, b in itertools.pairwise(signs) if a != b)
    assert changes == engine.shake_swings - 1


def test_gestures_complete_and_hold_neutral() -> None:
    """After the one-shot completes, every servo is held at neutral (additional steps don't move it)."""
    for motion in (Motion.NOD, Motion.HEAD_SHAKE):
        engine = MotionEngine()
        engine.motion = motion
        for _ in range(GESTURE_STEPS):
            engine.step()
        rest = engine.neck_pitch_center()
        tail = [engine.step() for _ in range(30)]
        for f in tail:
            assert f["neck_pitch1"] == rest, motion.name
            assert all(tick == engine.NEUTRAL for name, tick in f.items() if name != "neck_pitch1"), motion.name


def test_gesture_then_idle_returns_neutral() -> None:
    """Switching to IDLE mid-gesture still smoothly returns to neutral."""
    engine = MotionEngine()
    engine.motion = Motion.NOD
    for _ in range(10):  # cut it off mid-way through the first stroke
        engine.step()
    engine.motion = Motion.IDLE
    for _ in range(60):
        engine.step()
    pos = engine.get_positions()
    assert pos["neck_pitch1"] == engine.neck_pitch_center()
    assert all(tick == engine.NEUTRAL for name, tick in pos.items() if name != "neck_pitch1")


def test_gesture_restarts_on_reselect() -> None:
    """Re-setting after completion replays from the first stroke (same convention as wave)."""
    engine = MotionEngine()
    engine.motion = Motion.NOD
    for _ in range(GESTURE_STEPS):
        engine.step()
    engine.motion = Motion.NOD  # re-fire
    frames = [engine.step() for _ in range(30)]
    assert any(f["neck_pitch1"] != engine.NEUTRAL for f in frames)


def test_gesture_blends_offcenter_head_home() -> None:
    """Firing while looking away doesn't snap, and it faces forward when it finishes."""
    engine = MotionEngine()
    engine.set_neck(yaw=1.0)
    for _ in range(60):  # turn the neck fully left (+300) beforehand
        engine.step()
    before = engine.get_positions()["neck_yaw"]
    assert before != engine.NEUTRAL
    engine.motion = Motion.NOD
    first = engine.step()["neck_yaw"]
    assert abs(first - before) < 30  # no jump on the first frame
    for _ in range(GESTURE_STEPS):
        engine.step()
    pos = engine.get_positions()
    assert pos["neck_yaw"] == engine.NEUTRAL  # returns to facing forward
    assert pos["neck_pitch1"] == engine.neck_pitch_center()


def test_gesture_amp_is_clamped_to_neck_limit() -> None:
    """Even a wildly abused amplitude knob is clamped to the neck's safe amplitude (±300 ticks)."""
    engine = MotionEngine()
    engine.shake_amp_deg = 90.0  # clearly excessive
    engine.motion = Motion.HEAD_SHAKE
    for _ in range(GESTURE_STEPS):
        pos = engine.step()
        assert abs(pos["neck_yaw"] - engine.NEUTRAL) <= 300


def test_nod_first_down_is_stretched() -> None:
    """The first stroke's downswing is nod_first_down_scale times slower (softening the initial motion)."""
    slow, ref = MotionEngine(), MotionEngine()
    slow.nod_first_down_scale = 2.0
    ref.nod_first_down_scale = 1.0
    for engine in (slow, ref):
        engine.motion = Motion.NOD
    slow_off = [slow.step()["neck_pitch1"] - slow.NEUTRAL for _ in range(GESTURE_STEPS)]
    ref_off = [ref.step()["neck_pitch1"] - ref.NEUTRAL for _ in range(GESTURE_STEPS)]
    # the bottom of the dip (max offset) arrives later by the scale factor, but start latency is unchanged
    assert slow_off.index(max(slow_off)) > ref_off.index(max(ref_off))
    assert slow_off[2] != 0 and ref_off[2] != 0  # both start moving immediately


def test_neck_rest_pitch_trim_shifts_front() -> None:
    """neck_rest_pitch_deg raises both idle's neck height and the gesture's return point.

    Positive values raise the head (since positive ticks mean downward on
    hardware, the tick itself moves in the decreasing direction).
    """
    engine = MotionEngine()
    engine.neck_rest_pitch_deg = 5.0
    trimmed = engine.NEUTRAL - round(5.0 * engine.TICK_PER_DEG)
    for _ in range(60):  # idle: the neck smoothly settles at the trimmed center
        engine.step()
    assert engine.get_positions()["neck_pitch1"] == trimmed
    engine.motion = Motion.NOD
    offsets = [engine.step()["neck_pitch1"] - trimmed for _ in range(GESTURE_STEPS)]
    assert max(offsets) > 0  # the nod goes downward from the trim position
    for _ in range(30):
        engine.step()
    pos = engine.get_positions()
    assert pos["neck_pitch1"] == trimmed  # it returns to the trim position too
    assert pos["neck_yaw"] == engine.NEUTRAL


def test_gesture_from_deflected_head_stays_in_safe_band() -> None:
    """Even firing from a fully-deflected neck, blend residual + swing combined never exceeds ±300 ticks."""
    engine = MotionEngine()
    engine.set_neck(yaw=1.0)
    for _ in range(60):  # deflect neck yaw fully to +300
        engine.step()
    engine.motion = Motion.HEAD_SHAKE  # swing 1 is on the + side = same sign as the residual
    for _ in range(GESTURE_STEPS):
        pos = engine.step()
        assert abs(pos["neck_yaw"] - engine.NEUTRAL) <= 300
        assert abs(pos["neck_pitch1"] - engine.neck_pitch_center()) <= 300


def test_look_pitch_stays_in_servo_band_despite_rest_trim() -> None:
    """Even with rest trim + full swing, pitch never leaves the servo-neutral ±300 band (the band is a mechanical bound)."""
    engine = MotionEngine()  # default rest=15° shifts the center upward
    # full swing in the same direction as the trim → -471 ticks with a naive implementation
    engine.set_neck(pitch=-1.0)
    for _ in range(120):
        engine.step()
    pos = engine.get_positions()
    assert pos["neck_pitch1"] >= engine.NEUTRAL - engine._neck_amplitude


def test_gesture_pitch_stays_in_servo_band_despite_rest_trim() -> None:
    """Even during a gesture, pitch's absolute tick stays within the servo-neutral ±300 band."""
    engine = MotionEngine()
    engine.set_neck(pitch=-1.0)  # fire from a state fully deflected upward
    for _ in range(120):
        engine.step()
    engine.motion = Motion.NOD
    for _ in range(GESTURE_STEPS):
        pos = engine.step()
        assert engine.NEUTRAL - engine._neck_amplitude <= pos["neck_pitch1"] <= engine.NEUTRAL + engine._neck_amplitude


def test_blend_finishes_by_first_keyframe() -> None:
    """Even with an extremely short period, the blend finishes by the first stroke's peak (prevents distortion).

    Fires with an extremely short period from a fully-deflected neck, and
    confirms the blend residual has vanished by the frame that reaches the
    first stroke's peak (i.e. the peak value matches the keyframe).
    """
    engine = MotionEngine()
    engine.neck_rest_pitch_deg = 0.0  # look at amplitude alone from a plain center
    engine.shake_period = 0.24  # first stroke 0.06s < blend window 0.2s
    engine.set_neck(yaw=1.0)
    for _ in range(60):
        engine.step()
    engine.motion = Motion.HEAD_SHAKE
    offsets = [engine.step()["neck_yaw"] - engine.NEUTRAL for _ in range(GESTURE_STEPS)]
    amp = engine.shake_amp_deg * engine.TICK_PER_DEG
    # stroke 2 (- side) peak: with zero residual it reaches -amp*decay, matching the keyframe
    assert min(offsets) == pytest.approx(-amp * engine.shake_decay, abs=6)


def test_gesture_mid_gait_settles_legs() -> None:
    """Firing a gesture mid-walk doesn't freeze the legs — they smoothly return to the neutral stance."""
    engine = MotionEngine()
    engine.motion = Motion.FORWARD
    for _ in range(30):
        engine.step()
    engine.motion = Motion.NOD
    for _ in range(90):
        pos = engine.step()
    assert all(pos[f"leg_{i}_{s}"] == engine.NEUTRAL for i in range(1, 7) for s in ("yaw", "pitch1", "pitch2"))


def test_neck_rest_trim_is_clamped() -> None:
    """Even a rest-trim typo (e.g. 90°) clamps the center shift to ±20°."""
    engine = MotionEngine()
    engine.neck_rest_pitch_deg = 90.0
    assert engine.neck_pitch_center() == engine.NEUTRAL - round(20.0 * engine.TICK_PER_DEG)


def test_gesture_seconds_matches_timeline() -> None:
    """gesture_seconds matches the timeline's endpoint, and is None for non-gesture motions."""
    engine = MotionEngine()
    assert engine.gesture_seconds(Motion.NOD) == engine._build_nod_timeline()[-1][0]
    assert engine.gesture_seconds(Motion.HEAD_SHAKE) == engine._build_shake_timeline()[-1][0]
    assert engine.gesture_seconds(Motion.FORWARD) is None


def test_set_neck_positions_seeds_only_neck_keys() -> None:
    """set_neck_positions writes only neck_* keys and ignores leg keys."""
    engine = MotionEngine()
    engine.set_neck_positions({"neck_pitch1": 1877, "neck_yaw": 2100, "leg_1_yaw": 9999})
    pos = engine.get_positions()
    assert pos["neck_pitch1"] == 1877
    assert pos["neck_yaw"] == 2100
    assert pos["leg_1_yaw"] == engine.NEUTRAL


# WAVE — official single-arm gesture
# ================================================================


def _play_wave(engine: MotionEngine, leg: int, steps: int = 500) -> list[dict[str, int]]:
    """Run the official single-arm wave and return all position frames."""
    engine.wave_leg = leg
    engine.motion = Motion.WAVE
    return [dict(engine.step()) for _ in range(steps)]


def test_wave_defaults_match_official_hardware_tuning() -> None:
    """single wave's defaults preserve the official, hardware-approved tuning."""
    engine = MotionEngine()
    assert engine.wave_speed == 1.9
    assert engine.wave_intro_speed == 2.3
    assert engine.wave_raise_speed == 0.6
    assert engine.wave_size == 160.0
    assert engine.wave_decay == 0.8
    assert engine.wave_count == 2
    assert engine.wave_period == 1.0
    assert engine.wave_dwell == 0.6


@pytest.mark.parametrize("leg", [3, 6], ids=["left", "right"])
def test_wave_completes_safely_and_holds_neutral(leg: int) -> None:
    """Both left and right official waves complete within the safe range and settle back to neutral on all legs."""
    engine = MotionEngine()
    frames = _play_wave(engine, leg)
    arm_keys = [f"leg_{leg}_{axis}" for axis in ("yaw", "pitch1", "pitch2")]
    assert any(any(frame[key] != engine.NEUTRAL for key in arm_keys) for frame in frames)
    for frame in frames:
        for name, tick in frame.items():
            assert SAFE_LO <= tick <= SAFE_HI, f"{name}={tick} out of range"
    assert all(tick == engine.NEUTRAL for key, tick in frames[-1].items() if key.startswith("leg_"))
    assert frames[-1] == frames[-2]


@pytest.mark.parametrize(
    "knob,value",
    [
        ("wave_intro_speed", 0.0),
        ("wave_intro_speed", -5.0),
        ("wave_raise_speed", 0.0),
        ("wave_raise_speed", -5.0),
        ("wave_speed", 0.0),
        ("wave_speed", -5.0),
    ],
)
def test_wave_rate_knob_zero_or_negative_cannot_reverse_or_freeze(knob: str, value: float) -> None:
    """Even with single wave's speed knob at 0/negative, the clock still advances forward."""
    engine = MotionEngine()
    setattr(engine, knob, value)
    engine.motion = Motion.WAVE
    for _ in range(200):
        engine.step()
    assert engine._wave_phase > 0.05


@pytest.mark.parametrize(
    "count,period,dwell,decay",
    [
        (0, 1.0, 0.6, 0.8),
        (2, 0.0, 0.6, 0.8),
        (2, 1.0, -1.0, -1.0),
        (2, 1.0, 2.0, 2.0),
    ],
)
def test_wave_shape_knob_extremes_stay_safe_and_finish(
    count: int,
    period: float,
    dwell: float,
    decay: float,
) -> None:
    """Out-of-range input to the timeline knobs is clamped too, and the motion still returns safely to neutral."""
    engine = MotionEngine()
    engine.wave_count = count
    engine.wave_period = period
    engine.wave_dwell = dwell
    engine.wave_decay = decay
    frames = _play_wave(engine, 3)
    for frame in frames:
        assert all(SAFE_LO <= tick <= SAFE_HI for tick in frame.values())
    assert all(tick == engine.NEUTRAL for key, tick in frames[-1].items() if key.startswith("leg_"))


# ================================================================
# WAVE_BOTH — two-handed wave on the beg stance
# ================================================================


def _play_wave_both(engine: MotionEngine, steps: int = 400) -> list[dict[str, int]]:
    """Run WAVE_BOTH to completion, returning the per-step position dicts."""
    engine.motion = Motion.WAVE_BOTH
    return [dict(engine.step()) for _ in range(steps)]


def test_wave_both_pose_leads_the_arms() -> None:
    """The beg posture (mid leg pushed forward) is already built before the arms lift off.

    One cause of body wobble in prototyping was the arm lifting while the
    support quadrilateral's front edge was still over the CoG (an overlap
    between the posture env and the arm timeline). Confirm that by the frame
    the arms first move, the mid legs are already nearly at their hold value.
    """
    engine = MotionEngine()
    frames = _play_wave_both(engine)
    n = engine.NEUTRAL
    arm_keys = [f"leg_{i}_{ax}" for i in (3, 6) for ax in ("yaw", "pitch1", "pitch2")]
    first_arm = next(i for i, f in enumerate(frames) if any(f[k] != n for k in arm_keys))
    # the mid legs' (2/5) hold value = the beg posture at env=1. It stays constant
    # during the hold, so it can be sampled from a frame mid-way through the arm stroke.
    mid_hold = {k: frames[first_arm + 30][k] for k in ("leg_2_yaw", "leg_5_yaw")}
    at_liftoff = frames[first_arm]
    for k, held in mid_hold.items():
        assert held != n, "mid leg yaw isn't moving in the beg posture"
        progress = (at_liftoff[k] - n) / (held - n)
        assert progress >= 0.95, f"{k}: beg posture is only {progress:.0%} built at arm liftoff"


def test_wave_both_mid_feet_carry_the_front_edge() -> None:
    """During the hold, the mid legs' ticks are exactly the lean+mid_forward IK solution.

    Pushing the mid legs forward is the core stabilization that moves the
    support quadrilateral's front edge ahead of the CoG. Matching the engine's
    output to kinematics' IK pins down the dx split (mid legs =
    lean+mid_forward, rear legs = lean only).
    """
    from palmimo_sdk import kinematics

    engine = MotionEngine()
    frames = _play_wave_both(engine)
    # the frame where the arm is highest = mid-stroke (posture env is definitely 1.0)
    peak = max(range(len(frames)), key=lambda i: abs(frames[i]["leg_3_pitch1"] - engine.NEUTRAL))
    dx_mid = engine.wave_both_lean + engine.wave_both_mid_forward
    dx_rear = engine.wave_both_lean
    for leg, dx, dz in (
        (2, dx_mid, 0.0),
        (5, dx_mid, 0.0),
        (1, dx_rear, engine.wave_both_noseup),
        (4, dx_rear, engine.wave_both_noseup),
    ):
        expect = kinematics.leg_servo_ticks(leg, dx, 0, dz)
        for key, tick in expect.items():
            assert frames[peak][key] == tick, f"{key}: {frames[peak][key]} != {tick}"


def test_wave_both_is_left_right_symmetric() -> None:
    """In simultaneous mode (phase=0), left and right are mirror-symmetric on every frame (no lateral drift)."""
    engine = MotionEngine()
    engine.wave_both_phase = 0.0  # default is alternating (0.5), so make simultaneous mode explicit
    n = engine.NEUTRAL
    for f in _play_wave_both(engine):
        for left, right in ((3, 6), (2, 5), (1, 4)):
            assert f[f"leg_{left}_yaw"] - n == -(f[f"leg_{right}_yaw"] - n)
            # pitch_sign is flipped between left and right, so the ticks mirror too
            assert f[f"leg_{left}_pitch1"] - n == -(f[f"leg_{right}_pitch1"] - n)
            assert f[f"leg_{left}_pitch2"] - n == -(f[f"leg_{right}_pitch2"] - n)


def test_wave_both_returns_to_neutral_and_holds() -> None:
    """The gesture plays only once, and every servo is held at neutral after it finishes."""
    engine = MotionEngine()
    frames = _play_wave_both(engine, steps=500)
    tail = frames[-1]
    assert all(tick == engine.NEUTRAL for key, tick in tail.items() if key.startswith("leg_"))
    assert tail == frames[-2]  # doesn't move after finishing


def test_wave_both_reselect_restarts_from_the_ground() -> None:
    """Reselecting resets the phase — even mid-gesture, it always rebuilds the beg posture from scratch."""
    engine = MotionEngine()
    engine.motion = Motion.WAVE_BOTH
    for _ in range(120):
        engine.step()
    engine.motion = Motion.WAVE_BOTH  # reselect
    pos = engine.step()
    n = engine.NEUTRAL
    for i in (3, 6):  # arms are still grounded (pre-lift)
        assert abs(pos[f"leg_{i}_pitch1"] - n) <= 20


@pytest.mark.parametrize(
    "yaw,midfwd,size,lean,noseup,phase",
    [
        (0.0, 0.0, 110.0, 10.0, 8.0, 0.0),  # plain posture (all knobs conservative)
        (45.0, 60.0, 200.0, 25.0, 16.0, 0.5),  # aggressive values (knobs' assumed upper bound, alternating mode)
    ],
)
def test_wave_both_knob_extremes_stay_in_safe_range(
    yaw: float, midfwd: float, size: float, lean: float, noseup: float, phase: float
) -> None:
    """Even with the knobs cranked, every tick stays in the safe range (200–3900)."""
    engine = MotionEngine()
    engine.wave_both_yaw = yaw
    engine.wave_both_mid_forward = midfwd
    engine.wave_both_size = size
    engine.wave_both_lean = lean
    engine.wave_both_noseup = noseup
    engine.wave_both_phase = phase
    engine.motion = Motion.WAVE_BOTH
    for _ in range(500):
        pos = engine.step()
        for name, tick in pos.items():
            assert SAFE_LO <= tick <= SAFE_HI, f"{name}={tick} out of range"


def test_wave_both_yaw_extremes_respect_hand_separation_floor() -> None:
    """No matter how much wave_both_yaw is cranked, the hands never get closer than the 25mm separation floor.

    Before the clamp was introduced, yaw=45 (the "assumed upper bound" from
    the knob-extreme test) produced a foot-center distance of 0.11mm —
    a paddle collision confirmed by analysis.
    """
    import math

    for yaw, phase in ((45.0, 0.0), (60.0, 0.0), (45.0, 0.5)):
        engine = MotionEngine()
        engine.wave_both_yaw = yaw
        engine.wave_both_phase = phase
        engine.motion = Motion.WAVE_BOTH
        min_sep = 1e9
        for _ in range(500):
            f = dict(engine.step())
            left, right = _fk_foot(3, f), _fk_foot(6, f)
            if left[2] > -58.0 or right[2] > -58.0:
                min_sep = min(min_sep, math.dist(left, right))
        assert min_sep >= 24.0, f"yaw={yaw} phase={phase}: {min_sep:.2f}mm"


@pytest.mark.parametrize("height", [0.0, -50.0])
def test_clap_height_floor_keeps_the_sweep_off_the_ground(height: float) -> None:
    """Even with clap_height at 0/negative, the open/close sweep still happens above the floor (prevents floor scrubbing)."""
    engine = MotionEngine()
    engine.clap_height = height
    frames = _play_clap(engine)
    zs = [_fk_foot(3, f)[2] for f in frames]
    assert min(zs) >= -66.5  # never commands below ground level (-66mm)
    assert max(zs) >= -66.0 + 9.0  # lifts by at least the floor amount (10mm, less 1mm of tick slack) to open/close


@pytest.mark.parametrize(
    "knob,motion",
    [
        ("wave_both_intro_speed", Motion.WAVE_BOTH),
        ("wave_both_speed", Motion.WAVE_BOTH),
        ("clap_intro_speed", Motion.CLAP),
    ],
)
@pytest.mark.parametrize("value", [0.0, -5.0])
def test_rate_knob_zero_or_negative_cannot_freeze_the_gesture(knob: str, motion: Motion, value: float) -> None:
    """Even with a rate knob set to 0/negative, the clock still advances (prevents a permanently frozen prep or running backward)."""
    engine = MotionEngine()
    setattr(engine, knob, value)
    engine.motion = motion
    for _ in range(200):
        engine.step()
    assert engine._wave_phase > 0.3


def test_wave_both_phase_half_alternates_the_arms() -> None:
    """At the default (phase=0.5), strokes alternate (when one arm is at its peak, the other is down).

    In alternating mode, the lift gap near the peak opens up to roughly the
    stroke amplitude, and both arms return to neutral when it finishes. At
    phase=0, both arms are always at the same height (like a "hooray" pose).
    """
    engine = MotionEngine()  # default = alternating
    frames = _play_wave_both(engine, steps=500)
    max_gap = 0.0
    for f in frames:
        z3 = _fk_foot(3, f)[2]
        z6 = _fk_foot(6, f)[2]
        max_gap = max(max_gap, abs(z3 - z6))
    assert max_gap > engine.wave_both_size * 0.5  # alternating: there's a moment where one arm is up and the other down
    tail = frames[-1]
    assert all(tick == engine.NEUTRAL for key, tick in tail.items() if key.startswith("leg_"))

    # in simultaneous mode (phase=0), both arms are always at the same height
    engine2 = MotionEngine()
    engine2.wave_both_phase = 0.0
    frames2 = _play_wave_both(engine2, steps=400)
    for f in frames2:
        assert abs(_fk_foot(3, f)[2] - _fk_foot(6, f)[2]) < 2.0


# ================================================================
# CLAP — applause (bring both hands together in the beg posture without letting them touch)
# ================================================================


def _fk_foot(leg: int, ticks: dict[str, int]) -> tuple[float, float, float]:
    """Body-frame foot position from the shared forward kinematics."""
    from palmimo_sdk import kinematics as k

    return k.leg_forward_kinematics(leg, ticks)


def _play_clap(engine: MotionEngine, steps: int = 500) -> list[dict[str, int]]:
    engine.motion = Motion.CLAP
    return [dict(engine.step()) for _ in range(steps)]


def _hand_separations(frames: list[dict[str, int]]) -> list[tuple[float, bool]]:
    """(separation, hands_up) per frame, from forward kinematics."""
    import math

    out: list[tuple[float, bool]] = []
    for f in frames:
        left, right = _fk_foot(3, f), _fk_foot(6, f)
        sep = math.dist(left, right)
        out.append((sep, left[2] > -40.0))  # neutral foot tip z=-66; if raised, it's a hand
    return out


@pytest.mark.parametrize(
    "gap_knob,expected_floor",
    [
        # explicit value: closest approach = the knob value (shipped default is 20.0 = light-contact boundary)
        (25.0, 25.0),
        (0.0, 20.0),  # abused knob: bottoms out at the floor (20mm = light-tap boundary, the intended clap feel)
    ],
)
def test_clap_hands_never_close_beyond_gap(gap_knob: float, expected_floor: float) -> None:
    """The closest approach distance between the hands (front leg tips) never goes below clap_gap — i.e. they never touch."""
    engine = MotionEngine()
    engine.clap_gap = gap_knob
    frames = _play_clap(engine)
    min_sep = min(sep for sep, up in _hand_separations(frames) if up)
    assert min_sep >= expected_floor - 1.0  # margin for int-tick quantization


def test_clap_strokes_match_count() -> None:
    """The number of claps (local minima in separation) matches clap_count."""
    engine = MotionEngine()
    engine.clap_count = 4
    frames = _play_clap(engine, steps=600)
    seps = _hand_separations(frames)
    closes = sum(
        1
        for i in range(1, len(seps) - 1)
        if seps[i][1] and seps[i][0] < seps[i - 1][0] and seps[i][0] <= seps[i + 1][0] and seps[i][0] < 60.0
    )
    assert closes == 4


def test_clap_shares_the_beg_stance() -> None:
    """CLAP's support legs use the same beg posture as WAVE_BOTH (same knobs, same IK solution)."""
    from palmimo_sdk import kinematics

    engine = MotionEngine()
    frames = _play_clap(engine)
    # the frame where the hands are fully raised = posture env is 1.0
    peak = max(range(len(frames)), key=lambda i: _fk_foot(3, frames[i])[2])
    dx_mid = engine.wave_both_lean + engine.wave_both_mid_forward
    for leg, dx, dz in (
        (2, dx_mid, 0.0),
        (5, dx_mid, 0.0),
        (1, engine.wave_both_lean, engine.wave_both_noseup),
        (4, engine.wave_both_lean, engine.wave_both_noseup),
    ):
        expect = kinematics.leg_servo_ticks(leg, dx, 0, dz)
        for key, tick in expect.items():
            assert frames[peak][key] == tick


def test_clap_is_left_right_symmetric() -> None:
    """Left/right hands and support legs are mirror-symmetric on every frame (assuming lateral reaction forces cancel out)."""
    engine = MotionEngine()
    n = engine.NEUTRAL
    for f in _play_clap(engine):
        for left, right in ((3, 6), (2, 5), (1, 4)):
            assert f[f"leg_{left}_yaw"] - n == -(f[f"leg_{right}_yaw"] - n)
            assert f[f"leg_{left}_pitch1"] - n == -(f[f"leg_{right}_pitch1"] - n)
            assert f[f"leg_{left}_pitch2"] - n == -(f[f"leg_{right}_pitch2"] - n)


def test_clap_decay_tapers_the_reopen_but_not_the_gap() -> None:
    """clap_decay<1 only makes the re-opening swing shallower — the closest approach (gap) stays the same on every beat."""
    engine = MotionEngine()
    engine.clap_count = 3
    engine.clap_decay = 0.6
    # wider than the default (90) so the post-decay opening still crosses the detection threshold (60mm)
    engine.clap_open = 150.0
    frames = _play_clap(engine, steps=600)
    seps = _hand_separations(frames)
    # from the separation series while the hands are raised, pick out each beat's local max (open) and local min (close)
    up = [s for s, is_up in seps if is_up]
    opens, closes = [], []
    for i in range(1, len(up) - 1):
        if up[i] > up[i - 1] and up[i] >= up[i + 1] and up[i] > 60.0:
            opens.append(up[i])
        if up[i] < up[i - 1] and up[i] <= up[i + 1] and up[i] < 60.0:
            closes.append(up[i])
    assert len(opens) >= 2 and opens[1] < opens[0] * 0.85  # the re-opening decays
    assert max(closes) - min(closes) < 3.0  # the closing side is constant (=gap)


def test_clap_returns_to_neutral_and_holds() -> None:
    """Plays only once, and every leg is held at neutral after it finishes."""
    engine = MotionEngine()
    frames = _play_clap(engine, steps=600)
    tail = frames[-1]
    assert all(tick == engine.NEUTRAL for key, tick in tail.items() if key.startswith("leg_"))
    assert tail == frames[-2]


@pytest.mark.parametrize(
    "gap,open_,height,count,period",
    [
        (20.0, 200.0, 120.0, 5, 0.3),  # aggressive values
        (0.0, 10.0, 200.0, 1, 0.05),  # abused knobs (every clamp kicks in)
    ],
)
def test_clap_knob_extremes_stay_in_safe_range(
    gap: float, open_: float, height: float, count: int, period: float
) -> None:
    """No matter how the knobs are cranked, every tick stays in the safe range (200–3900)."""
    engine = MotionEngine()
    engine.clap_gap = gap
    engine.clap_open = open_
    engine.clap_height = height
    engine.clap_count = count
    engine.clap_period = period
    engine.motion = Motion.CLAP
    for _ in range(600):
        pos = engine.step()
        for name, tick in pos.items():
            assert SAFE_LO <= tick <= SAFE_HI, f"{name}={tick} out of range"
