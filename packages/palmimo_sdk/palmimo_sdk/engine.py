# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Palmimo Motion Engine — pure computation, zero I/O dependencies.

This module contains all inverse kinematics, gait generation, and servo
position computation for the Palmimo hexapod robot. It depends only on
the Python standard library (math).

Typical usage::

    engine = MotionEngine()
    engine.motion = Motion.FORWARD
    for _ in range(100):
        positions = engine.step()
        # positions is a dict like {"leg_1_yaw": 2048, ...}
"""

import math
from enum import Enum, auto
from itertools import pairwise
from typing import ClassVar

from . import kinematics


class Motion(Enum):
    """Available motion primitives."""

    IDLE = auto()
    FORWARD = auto()
    BACKWARD = auto()
    STRAFE_LEFT = auto()
    STRAFE_RIGHT = auto()
    ROTATE_LEFT = auto()
    ROTATE_RIGHT = auto()
    DANCE = auto()
    BODY_TILT = auto()
    PUSHUP = auto()
    WAVE = auto()
    WAVE_BOTH = auto()
    CLAP = auto()
    CREEP = auto()
    BOW = auto()
    STRETCH = auto()
    NOD = auto()
    HEAD_SHAKE = auto()


# The neck-only one-shot gestures — shared by the engine's dispatch and the
# facade's servo-tuning sync so the membership can't drift between them.
NECK_GESTURES: tuple[Motion, ...] = (Motion.NOD, Motion.HEAD_SHAKE)


class MotionEngine:
    """Stateful motion engine for the Palmimo hexapod.

    Holds the current gait phase, servo positions, and neck state.
    Call :meth:`step` each control cycle to advance the phase and
    get the next set of servo positions.

    All servo values are raw Dynamixel ticks (0-4095, center=2048).

    Args:
        gait_speed (float): Phase increment per step (0-1 range wraps). Default 0.012.
        step_length (float): Forward stride in mm. Default 30.
        step_height (float): Swing-leg lift height in mm for the walk and rotate gaits. Default 30.
        yaw_amplitude (int): Max yaw servo offset in ticks for walking. Default 250.
        dt (float): Seconds of gesture time per :meth:`step` call. Default ``1/60``;
            pass ``1/fps`` when stepping at a different control rate so the
            second-based posture one-shots stay wall-clock-true.
    """

    # Dynamixel constants and leg geometry — sourced from palmimo_sdk.kinematics,
    # the single source of truth for the IK math, and re-exported here for the
    # gait code below and for backward-compatible attribute access.
    NEUTRAL: int = kinematics.NEUTRAL
    DEG_PER_TICK: float = kinematics.DEG_PER_TICK
    TICK_PER_DEG: float = kinematics.TICK_PER_DEG

    # Neck mechanical travel: the safe amplitude (see ``_neck_amplitude`` below)
    # in raw ticks and its real-degree equivalent, PUBLIC so callers (e.g. the
    # facade's NeckPitchDegrees/NeckYawDegrees conversion, or an LLM tool's
    # schema range) can translate real degrees into the normalized [-1, 1]
    # `set_neck`/`look` input instead of guessing at the travel. Named per axis
    # (rather than one shared constant) so pitch and yaw can diverge later
    # without an API break -- today both read the same ``_neck_amplitude`` in
    # ``_apply_neck``/``_apply_neck_gesture``, so the two constants happen to
    # be equal, but nothing here assumes that stays true.
    NECK_AMPLITUDE_TICKS: ClassVar[int] = 300
    NECK_PITCH_TRAVEL_DEG: ClassVar[float] = NECK_AMPLITUDE_TICKS / TICK_PER_DEG  # ~26.37 deg
    NECK_YAW_TRAVEL_DEG: ClassVar[float] = NECK_AMPLITUDE_TICKS / TICK_PER_DEG  # ~26.37 deg
    L1: float = kinematics.L1
    L2: float = kinematics.L2
    L3: float = kinematics.L3
    THETA2_OFFSET: float = kinematics.THETA2_OFFSET
    THETA3_OFFSET: float = kinematics.THETA3_OFFSET
    LEG_MOUNT_ANGLE: ClassVar[dict[int, float]] = kinematics.LEG_MOUNT_ANGLE

    LEG_MOUNT_RADIUS: ClassVar[dict[int, float]] = kinematics.LEG_MOUNT_RADIUS

    # Leg names for human-readable output
    LEG_NAMES: ClassVar[dict[int, str]] = {
        1: "RL",
        2: "ML",
        3: "FL",
        4: "RR",
        5: "MR",
        6: "FR",
    }

    # Tripod gait groups
    TRIPOD_A: ClassVar[list[int]] = [1, 3, 5]  # RL, FL, MR
    TRIPOD_B: ClassVar[list[int]] = [2, 4, 6]  # ML, RR, FR

    LEFT_LEGS: ClassVar[list[int]] = list(kinematics.LEFT_LEGS)
    RIGHT_LEGS: ClassVar[list[int]] = list(kinematics.RIGHT_LEGS)

    ALL_LEGS: ClassVar[list[int]] = [1, 2, 3, 4, 5, 6]

    def __init__(
        self,
        gait_speed: float = 0.012,
        step_length: float = 30.0,
        step_height: float = 30.0,
        yaw_amplitude: int = 250,
        dt: float = 1.0 / 60.0,
    ):
        if dt <= 0:
            raise ValueError("dt must be positive.")
        self.gait_speed = gait_speed
        self.step_length = step_length
        self.step_height = step_height
        self.yaw_amplitude = yaw_amplitude
        # Seconds of gesture time one step() advances. The second-based posture
        # one-shots (bow, stretch) read this so their *_enter_s/*_hold_s/*_exit_s
        # knobs stay wall-clock-true at any control rate; pass 1/fps when
        # stepping at other than the 60 fps default. (The wave keeps its own
        # fixed _WAVE_DT clock: its rate knobs were hand-tuned as a package
        # against real-robot playback and are not safe to rescale blindly.)
        self.dt = dt

        # ---- internal state ----
        self._motion: Motion = Motion.IDLE
        self._gait_phase: float = 0.0
        self.wave_leg: int = 6  # leg ID to wave (1-6)
        # Wave playback rates (>1 = snappier) and swing size in mm — all
        # switchable at runtime. wave_speed times the up/down strokes; the
        # separate wave_intro_speed times the prep raise and the return, so a
        # slow intro + fast strokes provides deliberate contrast in pacing.
        # Set both equal for a uniform global speed.
        self.wave_speed: float = self._WAVE_SPEED
        self.wave_intro_speed: float = self._WAVE_INTRO_SPEED
        # Rate of just the FIRST big up-stroke (base -> peak), so the "raise the
        # arm" can be slow/deliberate while later waves stay quick.
        # Baked slower than wave_speed for the official deliberate raise.
        self.wave_raise_speed: float = self._WAVE_RAISE_SPEED
        self.wave_size: float = self._WAVE_SIZE
        # Per-wave amplitude decay: the first up-stroke swings the full wave_size,
        # each later one is multiplied by this factor (0..1). 1.0 = every wave the
        # same (old behavior); <1 = "raise big, then taper to small waves".
        self.wave_decay: float = self._WAVE_DECAY
        # Wave shape: number of up/down waves, seconds per up-down cycle, and the
        # dwell fraction (0 = continuous quick wiggle, higher = move-then-hold).
        self.wave_count: int = self._WAVE_COUNT
        self.wave_period: float = self._WAVE_PERIOD
        self.wave_dwell: float = self._WAVE_DWELL
        # Two-handed wave (WAVE_BOTH) posture knobs — runtime-settable so the
        # tip-margin can be dialed on hardware. lean = how far the body shifts
        # BACKWARD onto the mid/rear quad before both arms lift (mm of forward
        # stance-foot displacement); noseup = how far the rear corners drop to
        # pitch the body nose-up into a "beg" pose (mm). See _apply_wave_both.
        self.wave_both_lean: float = self._WAVE_BOTH_LEAN
        self.wave_both_noseup: float = self._WAVE_BOTH_NOSEUP
        # Inward yaw (deg) on each raised arm so the two hands angle toward the
        # front centre (closer together). See _apply_wave_both.
        self.wave_both_yaw: float = self._WAVE_BOTH_YAW
        # Extra forward shift (mm) of the MIDDLE stance feet only. This is the
        # main stability knob: it moves the stance quad's front edge (the
        # middle-feet line) ahead of the CoG, which the symmetric lean alone
        # cannot do without folding the rear knees. For the middle legs
        # (mounted at ±90°) a forward shift is almost a pure yaw swing, so it
        # costs no knee torque. See _apply_wave_both.
        self.wave_both_mid_forward: float = self._WAVE_BOTH_MID_FWD
        # Two-hand stroke size (mm) — separate from the single wave's
        # wave_size so the two-handed default can stay softer (both arms pump
        # the body in phase, so the same stroke disturbs pitch twice as hard).
        self.wave_both_size: float = self._WAVE_BOTH_SIZE
        # Prep/return rate for WAVE_BOTH only (the single wave keeps
        # wave_intro_speed) — a separate knob so it can be tuned independently
        # of the single-arm wave; defaults to the same rate (2.3).
        self.wave_both_intro_speed: float = self._WAVE_BOTH_INTRO_SPEED
        # Arm phase offset as a fraction of wave_period: 0 = both arms stroke
        # together (banzai), 0.5 = perfect alternation (right arm half a wave
        # behind the left). Alternation also CANCELS the arms' pitch reaction
        # instead of doubling it. See _apply_wave_both.
        self.wave_both_phase: float = self._WAVE_BOTH_PHASE
        # Two-hand stroke rate and per-wave decay — own knobs (NOT the shared
        # wave_speed/wave_decay) so the two-hand feel can be tuned without
        # touching the official single-arm wave.
        self.wave_both_speed: float = self._WAVE_BOTH_SPEED
        self.wave_both_decay: float = self._WAVE_BOTH_DECAY
        # Clap knobs — the hand open/close is specified in HAND SEPARATION mm
        # (centre-to-centre of the front feet), converted to arm azimuths by
        # the geometry in _apply_clap, so "stop a few mm short of touching" is
        # a direct knob. gap = closest approach (floored in code so knob abuse
        # can't slam the feet together); open = separation between claps.
        self.clap_count: int = self._CLAP_COUNT
        self.clap_period: float = self._CLAP_PERIOD
        self.clap_gap: float = self._CLAP_GAP
        self.clap_open: float = self._CLAP_OPEN
        self.clap_height: float = self._CLAP_HEIGHT
        self.clap_dwell: float = self._CLAP_DWELL
        # Per-beat reopen decay (1.0 = every beat reopens fully). <1 makes each
        # reopen shallower, starting from the FIRST reopen (the raise opens to
        # clap_open in full; the opening before beat 2 is already decay x) —
        # a decay control that keeps repeated beats from feeling mechanical. It
        # is neutral by default because a real clap does not taper much. The CLOSED end never moves,
        # so the gap floor is unaffected.
        self.clap_decay: float = self._CLAP_DECAY
        # Clap's own prep/return rate — kept separate from wave_both_intro_speed
        # because the two gestures want different paces (the clap's raise is
        # slower and more deliberate than the two-hand wave's); sharing the
        # knob would silently change one gesture's rate whenever the other's
        # tuning changes.
        self.clap_intro_speed: float = self._CLAP_INTRO_SPEED
        # Monotonic phase for the wave gesture, reset whenever a wave starts so
        # the eased lift always begins from the ground (see _apply_wave).
        self._wave_phase: float = 0.0

        # Bow greeting knobs — depth of the chest dip, how far the head dips,
        # and the enter/hold/exit spans (the tempo). All runtime-switchable.
        self.bow_depth: float = self._BOW_DEPTH_MM
        self.bow_neck_down: float = self._BOW_NECK_DOWN
        self.bow_enter_s: float = self._BOW_ENTER_S
        self.bow_hold_s: float = self._BOW_HOLD_S
        self.bow_exit_s: float = self._BOW_EXIT_S
        # Monotonic phase (seconds) for the bow, reset in the ``motion`` setter.
        self._bow_phase: float = 0.0

        # Stretch (tall rise) knobs — how far the femurs fold toward vertical,
        # the wind-up dip, the tempo, and the upward head reach. All
        # runtime-switchable.
        self.stretch_fold_deg: float = self._STRETCH_FOLD_DEG
        self.stretch_rise_s: float = self._STRETCH_RISE_S
        self.stretch_hold_s: float = self._STRETCH_HOLD_S
        self.stretch_settle_s: float = self._STRETCH_SETTLE_S
        self.stretch_neck_up: float = self._STRETCH_NECK_UP
        self.stretch_knee_ratio: float = self._STRETCH_KNEE_RATIO
        self.stretch_dip: float = self._STRETCH_DIP
        # Monotonic phase (seconds) for the stretch, reset in the ``motion`` setter.
        self._stretch_phase: float = 0.0

        # Nod / head-shake tuning knobs (amplitude, cycle period, stroke count,
        # per-stroke decay) — runtime-switchable, defaults in the _NOD_* /
        # _SHAKE_* ClassVars below.
        self.nod_amp_deg: float = self._NOD_AMP_DEG
        self.nod_period: float = self._NOD_PERIOD
        self.nod_count: int = self._NOD_COUNT
        self.nod_decay: float = self._NOD_DECAY
        self.nod_first_down_scale: float = self._NOD_FIRST_DOWN_SCALE
        self.shake_amp_deg: float = self._SHAKE_AMP_DEG
        self.shake_period: float = self._SHAKE_PERIOD
        self.shake_swings: int = self._SHAKE_SWINGS
        self.shake_decay: float = self._SHAKE_DECAY
        # Monotonic phase (s) for the neck gestures, reset whenever one starts,
        # and the neck pose captured at that moment so an off-center head can
        # be blended home instead of snapping (see _apply_neck_gesture).
        self._gesture_phase: float = 0.0
        self._gesture_neck_start: tuple[int, int] = (0, 0)

        # Servo positions (raw ticks)
        self._leg: dict[str, int] = {}
        for leg_id in range(1, 7):
            self._leg[f"leg_{leg_id}_yaw"] = self.NEUTRAL
            self._leg[f"leg_{leg_id}_pitch1"] = self.NEUTRAL
            self._leg[f"leg_{leg_id}_pitch2"] = self.NEUTRAL

        # Neck positions (raw ticks)
        self._neck: dict[str, int] = {
            "neck_yaw": self.NEUTRAL,
            "neck_pitch1": self.NEUTRAL,
        }

        # Neck limits
        self._neck_amplitude: int = self.NECK_AMPLITUDE_TICKS
        self._neck_step: int = 15

        # Resting pitch trim (deg; positive raises the gaze). Defines the
        # neck-pitch that counts as "front": idle look targets settle around
        # it and the nod/head-shake gestures oscillate from and return to it,
        # so the robot's default head height can sit above the servo neutral.
        self.neck_rest_pitch_deg: float = self._NECK_REST_PITCH_DEG

        # Neck target (normalized -1..1)
        self._neck_target_pitch: float = 0.0
        self._neck_target_yaw: float = 0.0

        # Dance: roll the body left/right (symmetric, so left/right legs mirror
        # and the load stays balanced — a pure lateral translation instead loads
        # one side and makes the robot tilt and sink).
        # With dance_level_head the body also lifts on a symmetric vertical term
        # so the head sways flat (constant height) instead of arcing. The vertical
        # lift is symmetric, so it does NOT reintroduce the one-sided fold. See
        # _apply_dance.
        # Dance knobs — defaults are the official hardware-tuned feel (see the
        # _DANCE_* ClassVars). All runtime-settable for re-tuning.
        self.dance_roll_deg: float = self._DANCE_ROLL_DEG  # body roll amplitude (degrees)
        self.dance_pivot_h: float = self._DANCE_PIVOT_H  # mm: roll-pivot height above the
        # coxa-axis plane. Sets the lateral head swing
        # = (dance_head_h - pivot_h)*sin(theta); near head
        # height keeps the head ~fixed in space.
        self.dance_head_h: float = self._DANCE_HEAD_H  # mm: head-centre height above the
        # coxa plane (CAD; URDF neck base +110mm + arm).
        # Used to hold the head level when leveling.
        self.dance_level_head: bool = self._DANCE_LEVEL_HEAD  # True: lift the body so the head
        # stays at a constant height (flat left/right sway).
        # False: arc (head dips at extremes) — the default.
        self.dance_dwell: float = self._DANCE_DWELL  # 0..~0.8: pause near each sway extreme. The
        # sine is steepened then clamped to ±1, so the body
        # sways over, HOLDS, then sways back. 0 = continuous.
        self.dance_speed: float = self._DANCE_SPEED  # dance tempo (own knob, NOT gait_speed; the
        # phase advances at dance_speed * 0.8 in _apply_dance)

    # ================================================================
    # PUBLIC API
    # ================================================================

    @property
    def motion(self) -> Motion:
        """Current motion primitive."""
        return self._motion

    @motion.setter
    def motion(self, value: Motion) -> None:
        # Restart the wave from the ground every time it's (re)selected — incl.
        # switching the waving leg — so the slow eased lift always plays. Dance
        # likewise restarts from phase 0 so the sway always begins at center
        # (same side first), which perform_dance's sway count relies on.
        if value in (Motion.WAVE, Motion.WAVE_BOTH, Motion.CLAP):
            self._wave_phase = 0.0
        elif value == Motion.DANCE:
            self._gait_phase = 0.0
        # Restart posture one-shots when (re)selected — but NEVER mid-gesture:
        # resetting a playing gesture's clock would command its t=0 pose in a
        # single frame (from the stretch's tall hold that is a ~50° fold under
        # full body load at the quick stretch profile — a fall risk). While a
        # gesture is playing, re-selecting it is a no-op; once it has finished
        # it replays from the top.
        if value == Motion.BOW and (self._motion != Motion.BOW or self._bow_phase >= self.bow_duration_s):
            self._bow_phase = 0.0
        if value == Motion.STRETCH and (
            self._motion != Motion.STRETCH or self._stretch_phase >= self.stretch_duration_s
        ):
            self._stretch_phase = 0.0
        # Leaving a posture one-shot mid-gesture (e.g. stop() during the hold):
        # recenter the neck target so IDLE brings the head back up along with
        # the legs, instead of freezing it at the interrupted dip.
        if self._motion in self._POSTURE_ONE_SHOTS and value != self._motion:
            self._neck_target_pitch = 0.0
            self._neck_target_yaw = 0.0
        # Restart a neck gesture from its first stroke every time it's
        # (re)selected, and remember where the head is so the gesture can blend
        # it back to center instead of snapping.
        if value in NECK_GESTURES:
            self._gesture_phase = 0.0
            self._gesture_neck_start = (
                self._neck["neck_pitch1"] - self.neck_pitch_center(),
                self._neck["neck_yaw"] - self.NEUTRAL,
            )
        self._motion = value

    @property
    def active_legs(self) -> set[int]:
        """Set of currently active leg IDs (for visualization)."""
        if self._motion == Motion.IDLE:
            return set()
        if self._motion == Motion.WAVE:
            return {self.wave_leg}  # only the chosen leg waves
        if self._motion in NECK_GESTURES:
            return set()  # neck-only gestures — legs only glide to stance
        if self._motion in (Motion.WAVE_BOTH, Motion.CLAP):
            return set(self._WAVE_BOTH_LEGS)  # both front legs are the hands
        return set(range(1, 7))

    @property
    def gait_phase(self) -> float:
        """Current gait phase in ``[0, 1)`` (read-only)."""
        return self._gait_phase

    def set_neck(self, pitch: float = 0.0, yaw: float = 0.0) -> None:
        """Set neck target position.

        Args:
            pitch (float): Normalized pitch in [-1, 1]. Positive tips the chin down.
            yaw (float): Normalized yaw in [-1, 1]. The sign-to-direction mapping is
                unspecified.
        """
        self._neck_target_pitch = max(-1.0, min(1.0, pitch))
        self._neck_target_yaw = max(-1.0, min(1.0, yaw))

    def step(self) -> dict[str, int]:
        """Advance one control cycle and return servo positions.

        Returns:
            dict[str, int]: Mapping of motor name to raw Dynamixel tick value.
                Keys: ``leg_{1-6}_{yaw,pitch1,pitch2}``, ``neck_yaw``,
                ``neck_pitch1``.
        """
        if self._motion == Motion.IDLE:
            self._apply_idle()
        elif self._motion == Motion.FORWARD:
            self._apply_tripod_gait(y_step=-self.step_length)
        elif self._motion == Motion.BACKWARD:
            self._apply_tripod_gait(y_step=self.step_length)
        elif self._motion == Motion.STRAFE_LEFT:
            self._apply_tripod_gait(x_step=self.step_length)
        elif self._motion == Motion.STRAFE_RIGHT:
            self._apply_tripod_gait(x_step=-self.step_length)
        elif self._motion == Motion.ROTATE_LEFT:
            self._apply_rotate(direction=1)
        elif self._motion == Motion.ROTATE_RIGHT:
            self._apply_rotate(direction=-1)
        elif self._motion == Motion.DANCE:
            self._apply_dance()
        elif self._motion == Motion.BODY_TILT:
            self._apply_body_tilt()
        elif self._motion == Motion.PUSHUP:
            self._apply_pushup()
        elif self._motion == Motion.WAVE:
            self._apply_wave()
        elif self._motion == Motion.WAVE_BOTH:
            self._apply_wave_both()
        elif self._motion == Motion.CLAP:
            self._apply_clap()
        elif self._motion == Motion.CREEP:
            self._apply_creep()
        elif self._motion == Motion.BOW:
            self._apply_bow()
        elif self._motion == Motion.STRETCH:
            self._apply_stretch()
        elif self._motion == Motion.NOD:
            # Ease the legs back to stance while the head gestures, so firing
            # mid-gait settles the legs (same glide as IDLE) instead of
            # freezing them mid-stride on lifted feet.
            self._apply_idle()
            self._apply_nod()
        elif self._motion == Motion.HEAD_SHAKE:
            self._apply_idle()
            self._apply_head_shake()

        # The neck gestures own the neck servos directly — the smoothing
        # toward the look target would fight their keyframes.
        if self._motion not in NECK_GESTURES:
            self._apply_neck()

        out: dict[str, int] = {}
        out.update(self._leg)
        out.update(self._neck)
        return out

    def get_positions(self) -> dict[str, int]:
        """Return current servo positions *without* advancing the phase."""
        out: dict[str, int] = {}
        out.update(self._leg)
        out.update(self._neck)
        return out

    def set_leg_positions(self, positions: dict[str, int]) -> None:
        """Seed the current leg tick state from an external pose.

        Use when another controller has been driving the legs directly and
        hands control back to the engine, so the next :meth:`step` interpolates
        from the real current pose instead of snapping from stale internal
        state. Only keys the engine owns (``leg_*``) are applied; others (e.g.
        ``neck_*``) are ignored.

        Args:
            positions (dict[str, int]): Motor name -> raw Dynamixel tick.
        """
        for key, tick in positions.items():
            if key in self._leg:
                self._leg[key] = int(tick)

    def set_neck_positions(self, positions: dict[str, int]) -> None:
        """Seed the current neck tick state from an external pose.

        Counterpart of :meth:`set_leg_positions` for the neck axes. Use when
        another controller (e.g. the facade's timed neutral glide) has moved
        the neck directly, so the next gesture blends from the real pose
        instead of a stale internal one. Only keys the engine owns
        (``neck_yaw`` / ``neck_pitch1``) are applied; others are ignored.

        Args:
            positions (dict[str, int]): Motor name -> raw Dynamixel tick.
        """
        for key, tick in positions.items():
            if key in self._neck:
                self._neck[key] = int(tick)

    def reset(self) -> None:
        """Reset all servos to neutral and clear motion state."""
        self._motion = Motion.IDLE
        self._gait_phase = 0.0
        for key in self._leg:
            self._leg[key] = self.NEUTRAL
        for key in self._neck:
            self._neck[key] = self.NEUTRAL
        self._neck_target_pitch = 0.0
        self._neck_target_yaw = 0.0

    # ================================================================
    # INVERSE KINEMATICS
    # ================================================================

    def _set_leg_from_ik(self, leg_id: int, dx: float, dy: float, dz: float) -> None:
        """Write a leg's servo ticks from a foot displacement via shared IK."""
        self._leg.update(kinematics.leg_servo_ticks(leg_id, dx, dy, dz))

    # ================================================================
    # GAIT IMPLEMENTATIONS
    # ================================================================

    def _apply_idle(self) -> None:
        """Smoothly return all legs to neutral."""
        for leg_id in range(1, 7):
            for suffix in ("yaw", "pitch1", "pitch2"):
                key = f"leg_{leg_id}_{suffix}"
                current = self._leg[key]
                if abs(current - self.NEUTRAL) < 10:
                    self._leg[key] = self.NEUTRAL
                else:
                    self._leg[key] += int((self.NEUTRAL - current) * 0.2)

    def _apply_tripod_gait(self, y_step: float = 0.0, x_step: float = 0.0) -> None:
        """IK lift + yaw stride tripod gait."""
        self._gait_phase = (self._gait_phase + self.gait_speed) % 1.0
        phase_rad = self._gait_phase * 2.0 * math.pi

        n = self.NEUTRAL
        z_lift = self.step_height

        for leg_id in range(1, 7):
            leg_phase = phase_rad if leg_id in self.TRIPOD_A else phase_rad + math.pi
            lp = leg_phase % (2.0 * math.pi)
            sin_val = math.sin(lp)
            cos_val = math.cos(lp)

            # Lift via IK
            dz = z_lift * sin_val if sin_val > 0 else -5.0 * (-sin_val)
            self._set_leg_from_ik(leg_id, 0, 0, dz)

            # Stride via yaw
            if abs(y_step) > 0:
                mount_sin = math.sin(math.radians(self.LEG_MOUNT_ANGLE[leg_id]))
                leg_yaw_amp = self.yaw_amplitude * abs(mount_sin)
                direction = 1 if y_step > 0 else -1
                yaw_sign = 1 if mount_sin >= 0 else -1
                self._leg[f"leg_{leg_id}_yaw"] = n + int(cos_val * leg_yaw_amp * direction * yaw_sign)
            elif abs(x_step) > 0:
                mount_cos = math.cos(math.radians(self.LEG_MOUNT_ANGLE[leg_id]))
                leg_yaw_amp = self.yaw_amplitude * abs(mount_cos)
                direction = 1 if x_step > 0 else -1
                yaw_sign = 1 if mount_cos >= 0 else -1
                self._leg[f"leg_{leg_id}_yaw"] = n + int(cos_val * leg_yaw_amp * direction * yaw_sign)

    def _apply_rotate(self, direction: int = 1) -> None:
        """Rotation gait: IK lift + uniform yaw."""
        self._gait_phase = (self._gait_phase + self.gait_speed) % 1.0
        phase_rad = self._gait_phase * 2.0 * math.pi

        n = self.NEUTRAL
        rotate_amplitude = 200
        z_lift = self.step_height

        for leg_id in range(1, 7):
            leg_phase = phase_rad if leg_id in self.TRIPOD_A else phase_rad + math.pi
            lp = leg_phase % (2.0 * math.pi)
            sin_val = math.sin(lp)

            dz = z_lift * sin_val if sin_val > 0 else -5.0 * (-sin_val)
            self._set_leg_from_ik(leg_id, 0, 0, dz)

            yaw_offset = int(math.cos(lp) * rotate_amplitude * direction)
            self._leg[f"leg_{leg_id}_yaw"] = n + yaw_offset

    def _apply_dance(self) -> None:
        """Dance: roll the body left/right so the head sways side to side, lifting
        the body so the head stays level ("dance from the hips", head horizontal).

        Symmetric roll (left/right legs mirror) keeps the load balanced. Modeled
        as a rigid-body move: roll by ``theta``, translate laterally by
        ``dance_pivot_h * sin(theta)`` (sets the head's lateral swing). The
        vertical term decides the head path:

        * ``dance_level_head``: ``z = -dance_head_h*(1-cos(theta))`` lifts
          the body exactly enough to cancel the head's vertical dip, so the head
          travels flat (constant height) left/right — the original goal. The lift
          is symmetric (all legs same), so it does not reintroduce the one-sided
          fold that a lateral translation causes.
        * otherwise (the shipped default): ``z = dance_pivot_h*(1-cos(theta))`` — the head arcs (dips at
          the extremes).

        Each foot target is ``R @ (base_foot_pos + t)`` via
        :func:`kinematics.leg_servo_ticks_body`. Amplitudes are hardware-tuned.
        """
        self._gait_phase = (self._gait_phase + self.dance_speed * 0.8) % 1.0
        phase_rad = self._gait_phase * 2.0 * math.pi

        # Dwell: steepen the sine then clamp to ±1 so the body reaches the extreme
        # quickly and HOLDS there, instead of a constant continuous wobble.
        sway = math.sin(phase_rad)
        if self.dance_dwell > 0.0:
            # Clamp <1 so a dwell of 1.0+ (set via API/CLI) can't divide by zero
            # or flip the sway; mirrors wave_dwell's clamp in _build_wave_timeline.
            dwell = min(0.99, self.dance_dwell)
            sway = max(-1.0, min(1.0, sway / (1.0 - dwell)))
        theta = math.radians(self.dance_roll_deg) * sway  # roll angle (rad)
        h = self.dance_pivot_h
        if self.dance_level_head:  # noqa: SIM108  (per-branch comments document the two modes)
            # Lift the body so the head holds a constant height (flat sway).
            z_t = -self.dance_head_h * (1.0 - math.cos(theta))
        else:
            # Original: head-height point is the pivot, head arcs at the extremes.
            z_t = h * (1.0 - math.cos(theta))
        body_pos = (0.0, h * math.sin(theta), z_t)
        R = kinematics.rotation_matrix(theta, 0.0, 0.0)
        for leg_id in range(1, 7):
            self._leg.update(kinematics.leg_servo_ticks_body(leg_id, body_pos, R))

    def _apply_body_tilt(self) -> None:
        """Body tilt: lean side to side by raising one side, lowering the other.

        Left legs lift up while right legs push down, then swap.
        Creates a rocking/curious expression.
        """
        self._gait_phase = (self._gait_phase + self.gait_speed * 0.6) % 1.0
        phase_rad = self._gait_phase * 2.0 * math.pi

        tilt = math.sin(phase_rad)
        lift_height = 30.0  # mm up for raised side
        sink_depth = -15.0  # mm down for lowered side

        for leg_id in range(1, 7):
            self._leg[f"leg_{leg_id}_yaw"] = self.NEUTRAL

            if leg_id in self.LEFT_LEGS:
                # Left side: positive tilt = lift left
                dz = tilt * lift_height if tilt > 0 else tilt * (-sink_depth)
            else:
                # Right side: positive tilt = sink right (opposite)
                dz = -tilt * lift_height if -tilt > 0 else -tilt * (-sink_depth)

            self._set_leg_from_ik(leg_id, 0, 0, dz)

    def _apply_pushup(self) -> None:
        """Push-up: all legs extend/retract together, raising and lowering the body.

        Uses IK to move all feet down (pushing body up) then back to neutral.
        """
        self._gait_phase = (self._gait_phase + self.gait_speed * 0.5) % 1.0
        phase_rad = self._gait_phase * 2.0 * math.pi

        # Smooth up/down motion: sin wave, biased so we spend more time up
        # Positive dz = foot goes up = body goes down
        # Negative dz = foot goes down = body goes up
        lift = math.sin(phase_rad)
        up_height = 35.0  # mm: how high the body rises (feet push down)
        down_depth = 15.0  # mm: how low the body dips (feet pull up)

        dz = -lift * up_height if lift > 0 else -lift * down_depth

        for leg_id in range(1, 7):
            self._leg[f"leg_{leg_id}_yaw"] = self.NEUTRAL
            self._set_leg_from_ik(leg_id, 0, 0, dz)

    # Wave gesture: a deliberate prep tilt, then punchy up/down waves that
    # move-then-hold, then a return — a discrete keyframe choreography, not a
    # smooth sinusoid.
    # Played once per wave (reset in the ``motion`` setter); holds neutral after.
    # The rate constants below are baked for the engine's 60 fps production
    # framerate. A tuning tool that reads servos back every frame runs slower
    # than production, so a rate tuned under readback needs dividing by the
    # production/tuning fps ratio to keep the same real-world duration at full
    # speed (see the per-axis "= tuned .../1.32" comments below).
    # Servo-side PV/gain/IIR tuning for this gesture is applied by the Palmimo
    # facade, not here (see Palmimo._sync_gesture_tuning).
    # Engine step (also drives the neck gestures) — fixed 60 fps tuning base.
    _WAVE_DT: ClassVar[float] = 1.0 / 60.0  # s/step (engine is 60 fps-tuned)
    _WAVE_SPEED: ClassVar[float] = 1.9  # rate of the up/down strokes (= tuned 2.5 / 1.32)
    _WAVE_INTRO_SPEED: ClassVar[float] = 2.3  # rate of the prep raise + return (= tuned 3.0 / 1.32)
    _WAVE_RAISE_SPEED: ClassVar[float] = 0.6  # rate of the FIRST big up-stroke (= tuned 0.8 / 1.32)
    _WAVE_REACH: ClassVar[float] = 40.0  # mm: lateral reach while waving
    _WAVE_TILT: ClassVar[float] = 30.0  # mm: body tilt toward the corner
    _WAVE_BASE: ClassVar[float] = 40.0  # mm: "down" rest height between waves
    _WAVE_SIZE: ClassVar[float] = 160.0  # mm: default wave swing above base
    _WAVE_DECAY: ClassVar[float] = 0.8  # per-wave amplitude multiplier: each wave 0.8x the last (raise big, then taper)
    # Wave-shape defaults (the choreography is generated from these, so count and
    # frequency are tunable): COUNT up/down waves, PERIOD seconds per up-down
    # cycle, DWELL = fraction of each half-cycle spent holding (0 = continuous
    # fine wiggle, higher = punchy move-then-hold).
    _WAVE_COUNT: ClassVar[int] = 2
    _WAVE_PERIOD: ClassVar[float] = 1.0
    _WAVE_DWELL: ClassVar[float] = 0.6

    # Two-handed wave (WAVE_BOTH): lift BOTH front legs (FL=3, FR=6) at once.
    # Unlike the single-arm wave (5 legs grounded), this leaves only the mid+rear
    # quad on the ground — and at neutral the quad's FRONT edge (the middle-feet
    # line) passes through the body centre, i.e. the static tip margin is ~zero.
    # Two things restore it:
    #   * MID_FWD walks the middle feet forward, moving the front edge itself
    #     ahead of the CoG. For the ±90°-mounted middle legs this is almost a
    #     pure yaw swing (vertical axis => no gravity torque, no knee change),
    #     so it buys margin for free — the main stability lever.
    #   * LEAN shifts the whole body backward. It also loads the rear knees,
    #     which fold fast (lean 10mm -> ~48deg knee under load; 25mm -> ~63deg),
    #     so keep it small and prefer MID_FWD for margin.
    # NOSEUP drops the rear corners into the nose-up "beg" look (also nudges the
    # CoG back a little). Verify tip margin + mid-leg servo heat on hardware
    # before opening the knobs up.
    _WAVE_BOTH_LEGS: ClassVar[tuple[int, int]] = (3, 6)  # FL, FR — the "hands"
    _WAVE_BOTH_MID: ClassVar[tuple[int, int]] = (2, 5)  # ML, MR — front edge of the stance quad
    _WAVE_BOTH_REAR: ClassVar[tuple[int, int]] = (1, 4)  # RL, RR — drop for nose-up
    _WAVE_BOTH_LEAN: ClassVar[float] = 10.0  # mm: stance feet shift forward => body backward
    _WAVE_BOTH_NOSEUP: ClassVar[float] = 8.0  # mm: rear corners drop => body pitches nose-up
    _WAVE_BOTH_MID_FWD: ClassVar[float] = 35.0  # mm: middle feet extra forward => front edge ahead of CoG
    # Inward yaw on each raised arm so the two hands angle toward the front centre
    # (closer together) instead of staying at the splayed mount angle (±32.2deg).
    # deg of azimuth swung inward; left arm +offset / right arm -offset both move
    # toward 0deg (forward). 0 = arms stay at the mount angle. NOTE: raising this
    # also moves the arms' mass forward, eating tip margin — if the body starts
    # bobbing at high yaw, raise wave_both_mid_forward with it.
    _WAVE_BOTH_YAW: ClassVar[float] = 15.0  # deg: inward arm yaw (hands closer together)
    # The two-hand default stroke is softer than the single wave's 160mm (in
    # the in-phase mode both arms pump the body pitch together). Defaults:
    # alternate by default (also cancels the arms' pitch reaction), quick
    # strokes with a light decay, and the prep/return at the single wave's clip.
    _WAVE_BOTH_SIZE: ClassVar[float] = 110.0  # mm: default two-hand swing above base
    _WAVE_BOTH_INTRO_SPEED: ClassVar[float] = 2.3  # rate of prep + return (= single wave)
    _WAVE_BOTH_PHASE: ClassVar[float] = 0.5  # arm offset in wave periods (0.5 = alternate)
    _WAVE_BOTH_SPEED: ClassVar[float] = 2.6  # stroke rate (single wave: 1.9)
    _WAVE_BOTH_DECAY: ClassVar[float] = 0.9  # per-wave amplitude multiplier (single: 0.8)
    # Hand-separation floor for the raised arms (mm, foot centres): the inward
    # yaw is clamped per frame so the two hands can never be commanded closer
    # than this, whatever wave_both_yaw says — without it yaw≈41°+ collides the
    # foot paddles (plates touch at ~20mm separation).
    # Same idea as _CLAP_GAP_FLOOR, but as a yaw clamp because the wave's knob
    # is angular. Sits ABOVE the plate-contact boundary.
    _WAVE_BOTH_SEP_FLOOR: ClassVar[float] = 25.0
    # Floor for the playback-rate knobs (intro/stroke). Rate 0 would freeze the
    # gesture mid-prep forever (with the arm PV=0/gain/IIR tuning left applied);
    # negative rates would run the clock backwards. 0.2 keeps even an abusive
    # setting finishing within ~30s.
    _MIN_RATE: ClassVar[float] = 0.2
    # Beg-pose lead (phase s): the arm timeline starts this much AFTER the
    # posture envelope, and the posture releases this-plus-return AFTER the
    # arms land. Without it the arms leave the ground when the beg stance is
    # only ~1/3 built (posture ramps over _WAVE_PREP_S=0.3 but the arm sweep
    # lifts off around t~0.1), i.e. exactly when the tip margin is still gone.
    _WAVE_BOTH_POSE_LEAD: ClassVar[float] = 0.35

    # Clap: on the same beg stance as WAVE_BOTH, raise both front legs and
    # swing the hands together/apart via their yaw axes to clap. The strokes are
    # mostly LATERAL and mirrored, so the reactions cancel side-to-side and the
    # clap disturbs pitch far less than the two-hand wave; the same beg-stance
    # knobs (wave_both_lean/noseup/mid_forward) provide the margin. The feet
    # NEVER touch: the closest approach is clap_gap mm between foot centres,
    # floored at _CLAP_GAP_FLOOR so no knob setting can drive a collision
    # (servo-impact load), and the conversion clamps inside the reachable arc.
    # Defaults are hardware-tuned: a quick 5Hz patter with a
    # narrow opening (small travel is what makes the fast tempo track — the
    # servos keep full amplitude) and the gap at the floor.
    _CLAP_COUNT: ClassVar[int] = 3  # close+open strokes per gesture
    _CLAP_PERIOD: ClassVar[float] = 0.2  # s per close+open cycle (~5 Hz clap tempo)
    _CLAP_GAP: ClassVar[float] = 20.0  # mm: closest foot-centre separation (= the floor)
    _CLAP_OPEN: ClassVar[float] = 90.0  # mm: separation between claps
    _CLAP_HEIGHT: ClassVar[float] = 90.0  # mm: hand lift above the neutral foot z
    _CLAP_DWELL: ClassVar[float] = 0.0  # fraction of each half-stroke held at the ends
    _CLAP_DECAY: ClassVar[float] = 1.0  # per-beat reopen multiplier (1 = no taper)
    # Height floor: without it clap_height=0 keeps the feet ON THE GROUND while
    # the separation still sweeps 249→20mm — the loaded front feet scrub the
    # floor at 5Hz with the crisp arm tuning fighting the friction; negative
    # values command below-ground targets (jacking / pitch-servo stall).
    _CLAP_HEIGHT_FLOOR: ClassVar[float] = 10.0  # mm: minimum hand lift
    # The wooden foot paddles are wider than the foot point: at gap=25 (foot
    # centres) the paddle faces are only a few mm apart, and they touch around
    # gap~20 — where the floor (and the default) sits BY DESIGN, since a light
    # paddle touch is the intended clap feel. The floor exists to stop anything
    # HARDER than that light touch (jamming the plates loads the yaw servos).
    # At the default 5Hz tempo the arm IIR (see robot._CLAP_ARM_IIR) lags enough
    # that the hands close to ~31mm rather than the commanded clap_gap; slower
    # tempos track the commanded value more closely.
    _CLAP_GAP_FLOOR: ClassVar[float] = 20.0  # mm: hard lower bound for clap_gap
    _CLAP_RAISE_S: ClassVar[float] = 0.5  # phase s: ground -> clap height, open
    _CLAP_INTRO_SPEED: ClassVar[float] = 1.2  # rate of prep/raise + lower/release (own knob)

    # Official dance feel: a small snappy body roll on an arcing head (level_head
    # off), with a brief hold at each extreme. dance_speed is the dance's OWN
    # tempo, kept separate from gait_speed so dialing the dance never changes the
    # walk gaits. The finished close (sway N times -> hold -> ease back) lives in
    # Palmimo.perform_dance.
    _DANCE_ROLL_DEG: ClassVar[float] = 5.0  # body roll amplitude (deg)
    _DANCE_PIVOT_H: ClassVar[float] = 125.0  # mm: roll-pivot height (~head height = head ~fixed)
    _DANCE_HEAD_H: ClassVar[float] = 191.0  # mm: head-centre height (only used when leveling)
    _DANCE_LEVEL_HEAD: ClassVar[bool] = False  # arc head (dips at extremes); flat-sway leveling off
    _DANCE_DWELL: ClassVar[float] = 0.2  # hold fraction at each extreme (0 = continuous)
    # dance tempo (advances at dance_speed * 0.8). 0.0149 = 0.017 scaled by the
    # readback-slowed tuning fps (~52.6/60) it was originally set at, so 60 fps
    # production (perform_dance / _pace) reproduces the same wall-clock speed
    # rather than running ~14% faster. A tuning tool paced like production
    # needs no such scaling.
    _DANCE_SPEED: ClassVar[float] = 0.0149
    # Fixed intro/outro spans (s): prep sweep up to base, settle hold, return.
    _WAVE_PREP_SWEEP: ClassVar[float] = 0.5
    _WAVE_SETTLE_DWELL: ClassVar[float] = 0.3
    _WAVE_RETURN_S: ClassVar[float] = 0.5  # also the tilt/reach ease-out span
    _WAVE_PREP_S: ClassVar[float] = 0.3  # tilt/reach ease-in span

    def _build_wave_timeline(
        self,
        decay: float | None = None,
    ) -> tuple[list[tuple[float, float]], float, float, float, float]:
        """Generate the wave keyframes from the count/period/dwell knobs.

        Returns ``(keys, intro_end, raise_end, outro_start, duration)`` where
        *keys* are ``(time_s, level)`` with level 0 = ground, 1 = "down" rest
        (base height), 2 = "up" wave peak (base + ``wave_size``). *raise_end* is
        the end of the first big up-stroke. *intro_end* / *outro_start*
        split the slow prep/return from the fast strokes for the variable-rate
        clock. Higher ``wave_dwell`` gives a punchy move-then-hold; 0 gives a
        continuous quick wiggle. *decay* overrides the per-wave amplitude
        multiplier (``None`` = the shared ``wave_decay``; the two-hand wave
        passes its own ``wave_both_decay``).
        """
        count = max(1, int(self.wave_count))
        period = max(0.05, float(self.wave_period))
        dwell_frac = min(0.95, max(0.0, float(self.wave_dwell)))
        half = period / 2.0
        move = half * (1.0 - dwell_frac)
        hold = half * dwell_frac

        keys: list[tuple[float, float]] = [(0.0, 0.0)]
        t = self._WAVE_PREP_SWEEP
        keys.append((t, 1.0))  # prep sweep up to base
        intro_end = t + self._WAVE_SETTLE_DWELL
        t = intro_end
        keys.append((t, 1.0))  # settle dwell before waving
        raise_end = intro_end  # end of the first (big) up-stroke
        # Amplitude scale of the current up-stroke as a fraction of wave_size; the
        # up keyframe level is 1.0 + amp (2.0 = full swing, 1.5 = half). It starts
        # full and decays each wave so the first raise is big and later waves taper.
        decay = max(0.0, min(1.0, float(self.wave_decay if decay is None else decay)))
        amp = 1.0
        for i in range(count):
            up_level = 1.0 + amp
            t += move  # wave up (smaller each cycle)
            keys.append((t, up_level))
            if i == 0:
                raise_end = t  # the big "raise the arm" finishes here
            if hold > 0.0:
                t += hold  # hold up
                keys.append((t, up_level))
            t += move  # wave down to base
            keys.append((t, 1.0))
            if hold > 0.0:
                t += hold  # hold down
                keys.append((t, 1.0))
            amp *= decay  # next wave swings less
        outro_start = t
        t += self._WAVE_RETURN_S
        keys.append((t, 0.0))  # return to neutral, then hold
        return keys, intro_end, raise_end, outro_start, t

    @staticmethod
    def _interp_keyframes(keys: list[tuple[float, float]], t: float) -> float:
        """Raised-cosine-interpolate a (time, value) keyframe list at time *t*.

        Before the first / after the last key, the endpoint value is held. Each
        segment eases in and out via ``0.5*(1-cos(pi*f))`` — a raised-cosine
        arc-length profile (``s=(L/2)(1-cos(pi*t/T))``) — so moves accelerate and
        stop cleanly while flat segments read as dwells.
        """
        if t <= keys[0][0]:
            return keys[0][1]
        for (t0, v0), (t1, v1) in pairwise(keys):
            if t <= t1:
                span = t1 - t0
                if span <= 0:
                    return v1
                f = (t - t0) / span
                f = 0.5 * (1.0 - math.cos(math.pi * f))  # raised-cosine ease
                return v0 + (v1 - v0) * f
        return keys[-1][1]

    def _apply_wave(self) -> None:
        """Wave one leg: a prep tilt, then punchy up/down waves that
        move-then-hold, then a return to neutral.

        Driven by ``self._wave_phase`` (seconds since the wave began, reset in
        the ``motion`` setter). The choreography is generated from the count/
        period/dwell knobs (:meth:`_build_wave_timeline`); the waving leg
        (``self.wave_leg``) follows it while reaching outward and the body tilts
        toward that corner. The whole gesture plays once and then holds neutral.
        """
        keys, intro_end, raise_end, outro_start, duration = self._build_wave_timeline()

        # Variable-rate clock: prep/return at wave_intro_speed (settle
        # into the corner), the FIRST big up-stroke at wave_raise_speed (raise the
        # arm slowly), then the rest of the up/down waves at wave_speed (quick).
        ph = self._wave_phase
        if ph >= outro_start or ph < intro_end:
            rate = max(self._MIN_RATE, float(self.wave_intro_speed))
        elif ph < raise_end:
            rate = max(self._MIN_RATE, float(self.wave_raise_speed))
        else:
            rate = max(self._MIN_RATE, float(self.wave_speed))
        self._wave_phase += self._WAVE_DT * rate
        t = self._wave_phase

        wave_leg = self.wave_leg
        # Keyframe level → mm: 0..1 ramps onto the base rest height, 1..2 swings
        # up by ``wave_size`` (the runtime knob for how big the wave is).
        level = self._interp_keyframes(keys, t)
        lift = level * self._WAVE_BASE if level <= 1.0 else self._WAVE_BASE + (level - 1.0) * self.wave_size

        # Reach + body tilt ease in during the prep, hold through the waves, and
        # ease out during the return — so the posture builds and releases with
        # the gesture instead of snapping on/off.
        if t < self._WAVE_PREP_S:
            env = t / self._WAVE_PREP_S
        elif t < duration - self._WAVE_RETURN_S:
            env = 1.0
        elif t < duration:
            env = (duration - t) / self._WAVE_RETURN_S
        else:
            env = 0.0
        env = max(0.0, min(1.0, env))

        # Neck stays at its (re-zeroed) default during the wave — the old
        # "look up" tilt was removed now that the neck's rest pose was retuned.
        self._neck_target_pitch = 0.0

        wave_is_right = wave_leg in self.RIGHT_LEGS
        dy_reach = (-self._WAVE_REACH if wave_is_right else self._WAVE_REACH) * env

        n = self.NEUTRAL

        for leg_id in range(1, 7):
            if leg_id == wave_leg:
                self._set_leg_from_ik(leg_id, 0, dy_reach, lift)
                self._leg[f"leg_{leg_id}_yaw"] = n
            else:
                # Tilt the body toward the waving corner: legs angularly close to
                # the wave leg rise, the diagonally opposite corner drops.
                wave_angle_rad = math.radians(kinematics.LEG_MOUNT_ANGLE[wave_leg])
                leg_angle_rad = math.radians(kinematics.LEG_MOUNT_ANGLE[leg_id])
                weight = math.cos(leg_angle_rad - wave_angle_rad)
                body_dz = -weight * self._WAVE_TILT * env
                self._set_leg_from_ik(leg_id, 0, 0, body_dz)
                self._leg[f"leg_{leg_id}_yaw"] = n

    def _max_inward_arm_yaw(self, dy_reach: float, lift: float) -> float:
        """Largest inward arm yaw (deg) keeping the hands ``_WAVE_BOTH_SEP_FLOOR`` apart.

        FK of the raised arm, mirroring the clap's separation math: the hand
        sits at horizontal radius *rho* from its yaw axis, so its body-frame
        lateral is ``coxa_y + rho*sin(azimuth)`` and the (symmetric worst-case)
        separation is twice that. Solving separation ≥ floor for the azimuth
        gives the inward-yaw ceiling. *rho* accounts for the outward reach and
        for the IK pulling the foot in when the lift approaches the leg's
        vertical limit (which is what let big yaws collide in practice).
        """
        leg = self._WAVE_BOTH_LEGS[0]  # FL: mount angle > 0
        base = kinematics.base_foot_pos(leg)
        rho = math.hypot(base[0], base[1] + abs(dy_reach))
        # Reach available at this height: past full extension the 2-link IK
        # clamps DIRECTION-preserving (the leg points at the target at full
        # length), so the foot's horizontal radius scales by chain/d — not the
        # z-preserving sqrt(chain²-tz²), which under-estimates rho and lets
        # the clamp pass collisions.
        tz = base[2] + lift
        chain = kinematics.L2 + kinematics.L3
        rh = rho - kinematics.L1
        d = math.hypot(rh, tz)
        rho_eff = kinematics.L1 + (rh * chain / d if d > chain else rh)
        coxa_y = self.LEG_MOUNT_RADIUS[leg] * math.sin(math.radians(self.LEG_MOUNT_ANGLE[leg]))
        s = (self._WAVE_BOTH_SEP_FLOOR / 2.0 - coxa_y) / max(rho_eff, 1.0)
        az_min = math.degrees(math.asin(max(-1.0, min(1.0, s))))
        return max(0.0, self.LEG_MOUNT_ANGLE[leg] - az_min)

    def _apply_beg_stance(self, env: float) -> None:
        """Write the 4 stance legs of the "beg" posture, scaled by *env* (0..1).

        The stance quad that keeps the robot up while both front legs are
        airborne (WAVE_BOTH, CLAP). Three ingredients, all eased by the shared
        prep/return envelope so the posture builds and releases smoothly:

        * ``wave_both_lean``: every stance foot moves forward (+dx, body frame)
          => the body shifts backward off the quad's front edge.
        * ``wave_both_mid_forward``: the MIDDLE feet move further forward,
          carrying the quad's front edge (the middle-feet line) ahead of the
          CoG. This is the tip margin: without it the front edge passes through
          the body centre and the arm strokes rock the body over it (the
          prototype's wobble). Mostly a yaw swing for the ±90° middle legs, so
          it adds no knee load.
        * ``wave_both_noseup``: the rear corners drop => nose-up beg look.

        dx is in the shared body frame (+x forward) so the same shift moves all
        four feet consistently. Unlike _apply_wave (pure-vertical tilt, yaw ~0),
        the horizontal shift needs the IK-computed yaw too — for the middle legs
        a forward shift IS mostly a yaw swing — so all three joints come from
        the IK and yaw is NOT pinned to neutral.
        """
        lean = self.wave_both_lean * env
        mid_fwd = self.wave_both_mid_forward * env
        noseup = self.wave_both_noseup * env
        for leg_id in range(1, 7):
            if leg_id in self._WAVE_BOTH_LEGS:
                continue  # airborne arms are the caller's business
            dx = lean + (mid_fwd if leg_id in self._WAVE_BOTH_MID else 0.0)
            dz = noseup if leg_id in self._WAVE_BOTH_REAR else 0.0
            self._set_leg_from_ik(leg_id, dx, 0, dz)

    def _apply_wave_both(self) -> None:
        """Wave BOTH front legs (FL=3, FR=6) at once — a two-handed greeting.

        The single-arm :meth:`_apply_wave` keeps 5 legs grounded, so it is
        trivially stable. Lifting both front legs leaves only the mid+rear quad
        on the ground with the CoG sitting right at its FRONT edge, so this would
        tip forward if the arms simply rose. The prep therefore builds the "beg"
        stance first (:meth:`_apply_beg_stance`: middle feet forward = front
        edge ahead of the CoG, small lean back, nose-up) — *then* both arms come
        up on the shared wave timeline.

        Reuses the wave choreography (:meth:`_build_wave_timeline`) and the same
        variable-rate clock and prep/return envelope shape as the single-arm
        wave, but with its own stroke knobs (``wave_both_size`` /
        ``wave_both_speed`` / ``wave_both_decay`` / ``wave_both_intro_speed``)
        so the two-hand feel is tuned independently of the official single-arm
        wave, and an arm phase offset (``wave_both_phase``, alternate by
        default). Verify tip margin and middle-leg servo heat on hardware
        before opening the knobs up.
        """
        keys, intro_end, raise_end, outro_start, duration = self._build_wave_timeline(decay=self.wave_both_decay)

        # The beg pose LEADS the arms: the arm timeline is shifted right by
        # POSE_LEAD so the stance is fully built before the arms leave the
        # ground, and the posture only releases after they land again. The
        # prototype overlapped the two, so the arms lifted off while the front
        # support edge still ran through the CoG — the tip margin existed only
        # mid-gesture, never at the handovers.
        lead = self._WAVE_BOTH_POSE_LEAD
        # Arm phase offset (timeline seconds): the SECOND arm (FR) reads the
        # shared timeline this far behind the first. 0 = strokes in unison;
        # 0.5 * period = perfect alternation, which also cancels the arms'
        # pitch reaction on the body instead of doubling it.
        off = max(0.0, min(1.0, float(self.wave_both_phase))) * max(0.05, float(self.wave_period))

        # Same pacing as the single-arm wave — slow prep/return, slow
        # first raise, quick strokes — but the prep/return rate is the two-hand
        # knob (gentler middle-feet drag; a more deliberate beg). The stroke
        # zone stretches by the arm offset so the delayed arm finishes its
        # waves before the return rate kicks in. (With an offset, the delayed
        # arm's own raise and the leading arm's return play at the stroke rate
        # — quicker than the opener, which reads fine mid-gesture.)
        ph = self._wave_phase
        if ph >= outro_start + lead + off or ph < intro_end + lead:
            rate = max(self._MIN_RATE, float(self.wave_both_intro_speed))
        elif ph < raise_end + lead:
            rate = self.wave_raise_speed
        else:
            rate = max(self._MIN_RATE, float(self.wave_both_speed))
        self._wave_phase += self._WAVE_DT * rate
        t = self._wave_phase

        # Posture envelope: beg stance eases in during prep, holds while the
        # arms are away (the delayed arm lands at duration + lead + off), then
        # eases out — the arms only ever move over a fully built stance.
        # Raised-cosine, not linear: a linear ramp reads as mechanical
        # so repeated motion feels less mechanical. The single wave keeps a
        # linear envelope because its motion profile was tuned with that shape.
        if t < self._WAVE_PREP_S:
            env = t / self._WAVE_PREP_S
        elif t < duration + lead + off:
            env = 1.0
        elif t < duration + lead + off + self._WAVE_RETURN_S:
            env = (duration + lead + off + self._WAVE_RETURN_S - t) / self._WAVE_RETURN_S
        else:
            env = 0.0
        env = 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, env)))

        self._neck_target_pitch = 0.0

        n = self.NEUTRAL
        for i, leg_id in enumerate(self._WAVE_BOTH_LEGS):
            # Keyframe level -> arm lift (mm), same mapping as _apply_wave but
            # on the two-hand stroke size. Each arm reads the shared timeline
            # at its own offset (interp holds level 0 before the first key, so
            # a delayed arm simply stays down longer).
            level = self._interp_keyframes(keys, t - lead - (off if i else 0.0))
            lift = level * self._WAVE_BASE if level <= 1.0 else self._WAVE_BASE + (level - 1.0) * self.wave_both_size
            # Reach/yaw follow the ARM (level), not the posture (env): with the
            # pose lead the posture is fully built while the hands are still
            # grounded, and env alone would drag them sideways across the floor.
            arm_env = env * max(0.0, min(1.0, level))
            # Waving arm: reach outward (mirrored by side) and follow the
            # up/down timeline. Airborne, so the stance shift doesn't apply.
            is_right = leg_id in self.RIGHT_LEGS
            dy_reach = (-self._WAVE_REACH if is_right else self._WAVE_REACH) * arm_env
            self._set_leg_from_ik(leg_id, 0, dy_reach, lift)
            # Angle the raised arm inward so the two hands come together at the
            # front centre. yaw tick = NEUTRAL - yaw_deg*TPD, and the target
            # azimuth = mount_angle - delta/TPD, so a +tick on the LEFT arm and
            # -tick on the RIGHT arm both swing toward 0deg (forward). Eased by
            # arm_env so the hands close as they rise and open as they lower.
            yaw_sign = 1 if leg_id in self.LEFT_LEGS else -1
            # Separation floor: clamp the effective inward yaw so the hands
            # can never be commanded into each other (see _WAVE_BOTH_SEP_FLOOR).
            yaw_in = min(self.wave_both_yaw * arm_env, self._max_inward_arm_yaw(dy_reach, lift))
            yaw_off = int(yaw_in * self.TICK_PER_DEG)
            self._leg[f"leg_{leg_id}_yaw"] = n + yaw_sign * yaw_off
        self._apply_beg_stance(env)

    def _apply_clap(self) -> None:
        """Clap on the beg stance by swinging both hands together and apart.

        Same support strategy as :meth:`_apply_wave_both` (beg stance via
        :meth:`_apply_beg_stance`, posture leading the arms by
        ``_WAVE_BOTH_POSE_LEAD``), but instead of pumping the arms vertically
        the hands travel along a horizontal arc: each foot keeps the leg's
        natural horizontal reach and slides around its yaw axis, so opening and
        closing is pure yaw — mirrored left/right, the stroke reactions cancel
        laterally and barely disturb pitch (gentler than the two-hand wave).

        The open/close is specified as HAND SEPARATION in mm (centre-to-centre
        of the two front feet): ``clap_open`` between claps, ``clap_gap`` at the
        closest approach. The feet never touch — ``clap_gap`` is floored at
        ``_CLAP_GAP_FLOOR`` and the arc conversion clamps to the reachable band,
        so no knob setting can slam the feet into each other (servo impact
        load). Sequence: build beg stance → raise the hands to ``clap_height``
        while they narrow to ``clap_open`` → ``clap_count`` close/open strokes
        at ``clap_period`` s each → lower the hands → release the stance.
        """
        lead = self._WAVE_BOTH_POSE_LEAD
        count = max(1, int(self.clap_count))
        period = max(0.1, float(self.clap_period))
        dwell = min(0.8, max(0.0, float(self.clap_dwell)))
        half = period / 2.0
        move = half * (1.0 - dwell)
        hold = half * dwell

        # Closure keyframes (0 = open, 1 = closed at clap_gap). interp holds
        # the 0 endpoint through the prep and raise segments. Every beat closes
        # all the way to the gap; with clap_decay < 1 each REOPEN is shallower
        # (the hands settle toward each other), so the taper never tightens the
        # closed end and the gap floor still holds.
        decay = max(0.0, min(1.0, float(self.clap_decay)))
        raise_end = lead + self._CLAP_RAISE_S
        keys: list[tuple[float, float]] = [(raise_end, 0.0)]
        t = raise_end
        reopen = 1.0
        for _ in range(count):
            t += move
            keys.append((t, 1.0))  # close
            if hold > 0.0:
                t += hold
                keys.append((t, 1.0))  # hold closed
            reopen *= decay
            t += move
            keys.append((t, 1.0 - reopen))  # reopen (shallower each beat)
            if hold > 0.0:
                t += hold
                keys.append((t, 1.0 - reopen))
        strokes_end = t
        lower_end = strokes_end + self._WAVE_RETURN_S  # hands back on the ground
        total = lower_end + self._WAVE_RETURN_S  # then the stance releases

        # Clock: slow prep/raise and lower/release (the clap's own intro rate,
        # floored so 0/negative can't freeze or reverse the gesture), the
        # strokes in real time so clap_period is wall-clock seconds.
        ph = self._wave_phase
        rate = max(self._MIN_RATE, float(self.clap_intro_speed)) if (ph < raise_end or ph >= strokes_end) else 1.0
        self._wave_phase += self._WAVE_DT * rate
        t_now = self._wave_phase

        # Posture envelope — same shape as _apply_wave_both: built before the
        # hands rise, held until they land, then released. Raised-cosine so
        # the beg stance settles in and releases organically.
        if t_now < self._WAVE_PREP_S:
            env = t_now / self._WAVE_PREP_S
        elif t_now < lower_end:
            env = 1.0
        elif t_now < total:
            env = (total - t_now) / self._WAVE_RETURN_S
        else:
            env = 0.0
        env = 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, env)))

        lift_lvl = self._interp_keyframes([(lead, 0.0), (raise_end, 1.0), (strokes_end, 1.0), (lower_end, 0.0)], t_now)
        closure = self._interp_keyframes(keys, t_now)

        self._neck_target_pitch = 0.0

        # Hand separation -> foot targets on the horizontal arc. The arc keeps
        # the neutral horizontal reach rho (so lift is pure dz for the IK) and
        # passes exactly through the neutral stance: separation at the mount
        # pose is 2*(coxa_y + base_y), which the lift blend starts from so the
        # grounded hands sit at neutral until they rise.
        base = kinematics.base_foot_pos(self._WAVE_BOTH_LEGS[0])  # FL: y > 0
        rho = math.hypot(base[0], base[1])
        coxa_y = self.LEG_MOUNT_RADIUS[self._WAVE_BOTH_LEGS[0]] * math.sin(
            math.radians(self.LEG_MOUNT_ANGLE[self._WAVE_BOTH_LEGS[0]])
        )
        natural_sep = 2.0 * (coxa_y + base[1])
        gap = max(self._CLAP_GAP_FLOOR, float(self.clap_gap))
        open_sep = max(gap, float(self.clap_open))
        sep_target = open_sep + (gap - open_sep) * closure
        sep = natural_sep + (sep_target - natural_sep) * lift_lvl
        # Foot y from the leg origin; clamped inside the arc so the sqrt and
        # the IK stay well-conditioned whatever the knobs say.
        ty_mag = min(max(sep / 2.0 - coxa_y, -0.9 * rho), 0.9 * rho)
        tx = math.sqrt(rho * rho - ty_mag * ty_mag)
        # Height floor: the separation sweep runs regardless of the lift, so an
        # unclamped 0/negative height would scrub the loaded feet across the
        # floor (or command below-ground targets). See _CLAP_HEIGHT_FLOOR.
        lift = lift_lvl * max(self._CLAP_HEIGHT_FLOOR, float(self.clap_height))

        for leg_id in self._WAVE_BOTH_LEGS:
            side = 1.0 if leg_id in self.LEFT_LEGS else -1.0
            dx = tx - base[0]
            dy = side * ty_mag - side * base[1]
            self._set_leg_from_ik(leg_id, dx, dy, lift)
        self._apply_beg_stance(env)

    def _apply_creep(self) -> None:
        """Creep gait: move one leg at a time in sequence.

        A wave gait where legs move in order: 1 → 3 → 5 → 2 → 4 → 6.
        Each leg lifts, strides forward via yaw, then plants. Much slower
        but more stable than tripod gait. Body always has 5 legs on ground.
        """
        # 6 legs, each gets 1/6 of the phase cycle
        self._gait_phase = (self._gait_phase + self.gait_speed * 0.4) % 1.0

        # Leg order: alternates sides for stability
        leg_order = [1, 6, 2, 5, 3, 4]  # RL, FR, ML, MR, FL, RR

        n = self.NEUTRAL
        creep_yaw_amp = 150  # ticks: smaller stride than tripod
        lift_height = 35.0  # mm

        for idx, leg_id in enumerate(leg_order):
            # Each leg is active during its 1/6 slice of the phase
            leg_start = idx / 6.0
            leg_end = (idx + 1) / 6.0

            # Is this leg currently in its swing phase?
            if leg_start <= self._gait_phase < leg_end:
                # Normalize to 0..1 within this leg's swing window
                t = (self._gait_phase - leg_start) / (1.0 / 6.0)
                swing_rad = t * math.pi  # 0 to pi

                # Lift: arc up then back down
                dz = lift_height * math.sin(swing_rad)
                self._set_leg_from_ik(leg_id, 0, 0, dz)

                # Yaw: sweep from back to front during swing
                yaw_val = int((t * 2.0 - 1.0) * creep_yaw_amp)  # -amp to +amp
                self._leg[f"leg_{leg_id}_yaw"] = n + yaw_val
            else:
                # Leg is in stance — slowly return yaw to neutral
                current_yaw = self._leg[f"leg_{leg_id}_yaw"]
                if abs(current_yaw - n) < 5:
                    self._leg[f"leg_{leg_id}_yaw"] = n
                else:
                    self._leg[f"leg_{leg_id}_yaw"] += int((n - current_yaw) * 0.05)
                # Keep feet on ground
                self._leg[f"leg_{leg_id}_pitch1"] = n
                self._leg[f"leg_{leg_id}_pitch2"] = n

    # Posture one-shots (bow, …): slow enter → hold → exit gestures driven by a
    # real-time clock, sharing the envelope skeleton below. Members reset their
    # phase clock in the ``motion`` setter and recenter the neck when left
    # mid-gesture.
    #
    # NOTE on the mm knobs: the neutral stance has the knee fully straight
    # (L2 + L3 colinear), so a command that would push a foot outside the
    # straight-leg sphere saturates — the shared IK clamps along the direction
    # to the target. Foot-down (hip-rise) and big slide+drop combos sit partly
    # in that saturation, so their effective offsets are smaller than the knob
    # value (the FK verification plots show the effective mm), and cranking a
    # saturated knob mostly re-aims the clamp direction instead of adding
    # reach. Foot-up (chest-dip) commands are inside the workspace and track
    # the knob 1:1.
    _POSTURE_ONE_SHOTS: ClassVar[frozenset[Motion]] = frozenset({Motion.BOW, Motion.STRETCH})

    # Bow greeting (chest dip + head dip → hold → slow rise): a slow posture
    # one-shot. The front feet rise (chest sinks toward the visitor) while the
    # rear feet drop a little (hips rise), pitching the body nose-down, and the
    # neck dips with it. Depth defaults are deliberately shallow: the bow tips
    # the body toward its front support edge, so the forward-tipping margin is
    # bought by starting conservative and raising the knobs on the real robot,
    # never by trusting untested deep values. Timing assumes the driver's
    # default time-based Profile_Velocity (300 = 300 ms per move); the strokes
    # are slow enough that no per-axis PV tuning is needed (unlike the wave).
    _BOW_DEPTH_MM: ClassVar[float] = 20.0  # mm: front feet rise = chest dip (shallow default)
    _BOW_NECK_DOWN: ClassVar[float] = 0.6  # normalized neck pitch dip (0..1)
    _BOW_ENTER_S: ClassVar[float] = 0.45  # s: ease down into the bow
    _BOW_HOLD_S: ClassVar[float] = 0.5  # s: hold the bow
    _BOW_EXIT_S: ClassVar[float] = 0.75  # s: slow respectful rise (total ~1.7 s)

    @property
    def bow_duration_s(self) -> float:
        """Full bow choreography length in seconds (from the live knobs)."""
        return self.bow_enter_s + self.bow_hold_s + self.bow_exit_s

    @staticmethod
    def _hold_envelope(t: float, enter_s: float, hold_s: float, exit_s: float) -> float:
        """0 → 1 → 0 envelope for enter/hold/exit posture one-shots.

        Raised-cosine eases into the pose over *enter_s*, holds it flat for
        *hold_s* (so the pose is perfectly still — no residual oscillation),
        eases back out over *exit_s*, then stays at 0 (the gesture is over).
        The shared skeleton for slow posture gestures like the bow.
        """
        keys = [
            (0.0, 0.0),
            (enter_s, 1.0),
            (enter_s + hold_s, 1.0),
            (enter_s + hold_s + exit_s, 0.0),
        ]
        return MotionEngine._interp_keyframes(keys, t)

    def _apply_bow(self) -> None:
        """Bow greeting: dip the chest and head, hold, then slowly rise.

        The legs shorten front-to-rear (front 1.0, middle 0.5, rear 0.0),
        tilting the body forward as a plane hinged on the planted rear feet,
        and the neck pitches down to make the greeting legible.
        Driven by ``self._bow_phase`` (seconds since the bow
        began, reset in the ``motion`` setter); plays once and holds neutral.
        """
        t = self._bow_phase
        self._bow_phase += self.dt
        env = self._hold_envelope(t, self.bow_enter_s, self.bow_hold_s, self.bow_exit_s)

        n = self.NEUTRAL
        # Front legs' mount cosine — the weight normalizer, taken from the
        # actual geometry so a mount-angle retune can't silently skew the
        # front-1.0 / rear-0.0 grading.
        front_cos = math.cos(math.radians(self.LEG_MOUNT_ANGLE[3]))
        for leg_id in range(1, 7):
            # +dz raises the foot relative to the body = that corner sinks.
            # Graded front 1.0 / middle 0.5 / rear 0.0: the body tilts forward
            # as a plane hinged on the REAR feet, so every foot stays planted
            # and loaded. (Commanding the rear feet downward instead would
            # saturate the IK at ~6 mm while the seesaw about the middle legs
            # lifts the rear end ~20 mm — the rear feet visibly floated.)
            forwardness = math.cos(math.radians(self.LEG_MOUNT_ANGLE[leg_id]))
            weight = 0.5 * (1.0 + forwardness / front_cos)
            dz = env * self.bow_depth * weight
            self._set_leg_from_ik(leg_id, 0, 0, dz)
            self._leg[f"leg_{leg_id}_yaw"] = n

        # Head bows with the body: a POSITIVE neck-pitch offset tips the chin
        # DOWN on this hardware, so the dip is positive.
        # The gesture owns the neck only WHILE it plays: once finished, the
        # look() targets belong to the caller again (a completed one-shot used
        # to keep zeroing them every tick, silently eating look()).
        if t < self.bow_duration_s:
            self._neck_target_pitch = env * self.bow_neck_down
            self._neck_target_yaw = 0.0

    # Stretch (tall rise: wind-up crouch, push the body up by folding every
    # femur toward vertical, hold, reverse): the "waking up / nobody has
    # talked to me for a while" idle gesture.
    #
    # The rise is a JOINT-space move on purpose: the shared IK models the
    # neutral knee as fully straight, so "extend the legs" saturates there —
    # but the physical knee sits visibly bent at neutral. Rotating pitch1
    # down by ``stretch_fold_deg`` (with pitch2 opening in step) bypasses the
    # model and actually raises the body: rise ≈ L2·(sin(40°+θ) − sin 40°),
    # and the femur sweep itself is the most camera-visible part.
    #
    # The feet NEVER leave the ground: the knee's small inward travel during
    # the fold is absorbed by the tall pads rolling in place and rolling back
    # (symmetric, self-reversing — the end pose matches the start within a
    # couple of camera pixels). Planted feet make the end pose exact by
    # construction, avoiding the accumulated pad-roll error an open-loop
    # stepping approach would introduce. The physical knee's range past the
    # model's "straight" is unknown; raise stretch_fold_deg gradually on the
    # real robot.
    _STRETCH_FOLD_DEG: ClassVar[float] = 50.0  # deg: femur fold; 50 = straight down (hand-verified range)
    _STRETCH_RISE_S: ClassVar[float] = 0.8  # s: body rise (and lower) span
    _STRETCH_HOLD_S: ClassVar[float] = 1.0  # s: hold the tall pose
    _STRETCH_SETTLE_S: ClassVar[float] = 0.2  # s: wind-up beat before the rise
    _STRETCH_NECK_UP: ClassVar[float] = 0.3  # normalized upward head reach while tall
    # Physical calibration: holding the shin at its natural ground angle opens
    # the knee ~0.82 deg per deg of femur fold (not 1.0). Separately, the femur
    # rests ~40 deg below horizontal — the IK model's THETA2_OFFSET (27 deg)
    # folds mechanical offsets into it and must not be used for real-world foot
    # geometry.
    _STRETCH_KNEE_RATIO: ClassVar[float] = 0.82
    # Anticipation dip: the wind-up crouch below neutral during the settle
    # beat (femurs lift, pushup-style), as a fold fraction, capped at 0.6.
    _STRETCH_DIP: ClassVar[float] = 0.15

    @property
    def stretch_duration_s(self) -> float:
        """Full stretch choreography length in seconds (from the live knobs)."""
        return 2.0 * self.stretch_rise_s + self.stretch_hold_s + self.stretch_settle_s

    def _apply_stretch(self) -> None:
        """Tall stretch on planted feet: wind-up dip, rise, hold, lower.

        Timeline: a settle beat sinks the body into a crouch, then every
        femur folds down together so the body rises (head reaching up), holds
        tall, and lowers back. The feet never leave their neutral footprint —
        the shins splay just enough to bridge the knee's inward travel.
        Driven by ``self._stretch_phase`` (seconds since the stretch began,
        reset in the ``motion`` setter); plays once and holds neutral.
        """
        t = self._stretch_phase
        self._stretch_phase += self.dt

        r, h = self.stretch_rise_s, self.stretch_hold_s
        t_rise = self.stretch_settle_s  # wind-up dip, then the rise
        t_hold = t_rise + r
        t_lower = t_hold + h

        fold_ticks = self.stretch_fold_deg * self.TICK_PER_DEG

        # Body rise envelope (fraction of the full fold, shared by all legs):
        # dip below neutral for the wind-up, spring up to 1, hold, ease back.
        dip_bottom = -min(0.6, max(0.0, self.stretch_dip))
        rise_env = self._interp_keyframes(
            [(0.0, 0.0), (t_rise, dip_bottom), (t_hold, 1.0), (t_lower, 1.0), (t_lower + r, 0.0)], t
        )

        # Femur down, knee opening by the calibrated natural ratio. The knee's
        # small inward travel is absorbed by the pads rolling in place and
        # rolling back (symmetric and self-reversing).
        knee_open = fold_ticks * self.stretch_knee_ratio * rise_env

        n = self.NEUTRAL
        for leg_id in range(1, 7):
            sign = 1 if leg_id in self.LEFT_LEGS else -1
            self._leg[f"leg_{leg_id}_yaw"] = n
            self._leg[f"leg_{leg_id}_pitch1"] = round(n - sign * fold_ticks * rise_env)
            self._leg[f"leg_{leg_id}_pitch2"] = round(n + sign * knee_open)

        # Head reaches UP with the rise (negative neck-pitch offset = chin up
        # on the real robot; see the sign note in _apply_bow). Only while the
        # gesture plays — afterwards look() owns the neck again.
        if t < self.stretch_duration_s:
            self._neck_target_pitch = -rise_env * self.stretch_neck_up
            self._neck_target_yaw = 0.0

    # ================================================================
    # NECK GESTURES (nod / head-shake)
    # ================================================================

    # Nod ("yes") and head-shake ("no") — quick neck-only one-shots meant to
    # answer a question, typically fired together with a matching face
    # expression. Both start their first stroke on the very first frame (no
    # prep phase — reply latency matters more than a wind-up), decay each
    # stroke so the gesture reads lively rather than metronomic, and end
    # facing front. The legs are never actively posed — they glide back to
    # the neutral stance exactly like IDLE, so firing from any pose (even
    # mid-gait) settles the robot instead of freezing lifted feet. Played
    # once (reset in the ``motion`` setter); holds center after. The two are tempo-matched (~1.3-1.5 s each, near-equal
    # stroke periods) so they read as a yes/no pair when used side by side.
    # Defaults: gentle first descent, large amplitude, near-uniform stroke size
    # (the raised neck rest adds to the presence).
    _NOD_AMP_DEG: ClassVar[float] = 25.0  # deg: first chin-down stroke
    _NOD_PERIOD: ClassVar[float] = 0.65  # s: one down-up cycle
    _NOD_COUNT: ClassVar[int] = 2  # down-up cycles per gesture
    _NOD_DECAY: ClassVar[float] = 0.95  # per-cycle amplitude multiplier
    # Duration multiplier for just the FIRST chin-down stroke (>1 = gentler
    # descent). The gesture still starts on frame one — only the speed of the
    # initial descent softens, echoing the wave's slow first raise.
    _NOD_FIRST_DOWN_SCALE: ClassVar[float] = 1.6
    _SHAKE_AMP_DEG: ClassVar[float] = 25.0  # deg: first sideways swing
    _SHAKE_PERIOD: ClassVar[float] = 0.66  # s: one full left-right-left cycle
    _SHAKE_SWINGS: ClassVar[int] = 4  # side peaks (4 = 2 round trips; odd values end half-way for a stronger "no-no")
    _SHAKE_DECAY: ClassVar[float] = 0.95  # per-swing amplitude multiplier
    # Span (s) over which a head that started off-center is blended back to
    # front at gesture start, so firing mid-look never snaps the neck.
    _GESTURE_BLEND_S: ClassVar[float] = 0.2
    # Default resting pitch trim (deg; positive raises the gaze — on this
    # hardware a positive tick offset tips the chin DOWN, so the trim
    # subtracts ticks). See ``neck_rest_pitch_deg``.
    _NECK_REST_PITCH_DEG: ClassVar[float] = 15.0
    # Hard limit on the rest trim (deg either way): a typo'd trim must not
    # relocate the neck's working center outside a sane mechanical window.
    _NECK_REST_LIMIT_DEG: ClassVar[float] = 20.0

    def neck_pitch_center(self) -> int:
        """Neck-pitch tick that counts as "front" (rest trim applied).

        Public because callers that reason about the neutral pose (e.g. the
        facade's return-to-neutral) must target this rather than the raw
        servo neutral. The trim is clamped to ±``_NECK_REST_LIMIT_DEG``.
        """
        lim = self._NECK_REST_LIMIT_DEG
        trim = max(-lim, min(lim, float(self.neck_rest_pitch_deg)))
        return self.NEUTRAL - round(trim * self.TICK_PER_DEG)

    def gesture_seconds(self, motion: Motion) -> float | None:
        """Choreography length (s) of a one-shot neck gesture, else ``None``.

        The single source of truth for "how long does this gesture play" —
        benches and callers should use this instead of re-deriving the length
        from the knobs.
        """
        if motion == Motion.NOD:
            return self._build_nod_timeline()[-1][0]
        if motion == Motion.HEAD_SHAKE:
            return self._build_shake_timeline()[-1][0]
        return None

    def _neck_amp_ticks(self, amp_deg: float) -> float:
        """Degrees -> neck tick offset, clamped to the neck's safe amplitude."""
        return min(float(self._neck_amplitude), abs(amp_deg) * self.TICK_PER_DEG)

    def _build_nod_timeline(self) -> list[tuple[float, float]]:
        """Nod keyframes as ``(time_s, neck-pitch tick offset)``.

        The chin dips and returns to center once per cycle; each later dip is
        ``nod_decay`` times the previous one, and the FIRST descent is
        stretched by ``nod_first_down_scale`` so the gesture opens gently
        instead of snapping down. A POSITIVE neck-pitch offset tips the chin
        down on this hardware.
        """
        period = max(0.05, float(self.nod_period))
        count = max(1, int(self.nod_count))
        decay = max(0.0, min(1.0, float(self.nod_decay)))
        first_scale = max(0.1, float(self.nod_first_down_scale))
        amp = self._neck_amp_ticks(self.nod_amp_deg)
        keys: list[tuple[float, float]] = [(0.0, 0.0)]
        t = 0.0
        for i in range(count):
            t += (period / 2.0) * (first_scale if i == 0 else 1.0)
            keys.append((t, amp))  # chin down (first one eased longer)
            t += period / 2.0
            keys.append((t, 0.0))  # back to center
            amp *= decay
        return keys

    def _build_shake_timeline(self) -> list[tuple[float, float]]:
        """Head-shake keyframes as ``(time_s, neck-yaw tick offset)``.

        Alternating side peaks with decay. The first move (center -> peak) and
        the final return (peak -> center) each take a quarter period; every
        peak-to-peak swing in between takes a half period, so the sweep speed
        stays constant. The default 4 swings = 2 decaying round trips; an odd
        ``shake_swings`` ends half-way through a round trip for a stronger
        "no-no" feel.
        """
        period = max(0.05, float(self.shake_period))
        swings = max(1, int(self.shake_swings))
        decay = max(0.0, min(1.0, float(self.shake_decay)))
        amp = self._neck_amp_ticks(self.shake_amp_deg)
        keys: list[tuple[float, float]] = [(0.0, 0.0)]
        t = 0.0
        side = 1.0
        for i in range(swings):
            t += period / 4.0 if i == 0 else period / 2.0
            keys.append((t, side * amp))
            side = -side
            amp *= decay
        t += period / 4.0
        keys.append((t, 0.0))  # settle back to front
        return keys

    def _apply_neck_gesture(self, keys: list[tuple[float, float]], axis: str) -> None:
        """Drive one neck *axis* through *keys* while easing the head home.

        Shared body of the nod / head-shake one-shots: the driven axis follows
        the keyframe offsets (raised-cosine interpolated); whatever off-center
        pose the head started with is blended out over the first
        ``_GESTURE_BLEND_S`` so the gesture can fire from any look direction
        without a snap and always ends facing front.
        """
        self._gesture_phase += self._WAVE_DT
        t = self._gesture_phase
        # The home blend must finish no later than the first keyframe, or the
        # fading residual would still be fighting the stroke past its peak
        # when the knobs make the first stroke shorter than the blend window.
        blend_s = min(self._GESTURE_BLEND_S, keys[1][0])
        w = 0.5 * (1.0 + math.cos(math.pi * t / blend_s)) if t < blend_s else 0.0
        start_pitch, start_yaw = self._gesture_neck_start
        offset = {"neck_pitch1": start_pitch * w, "neck_yaw": start_yaw * w}
        offset[axis] += self._interp_keyframes(keys, t)
        # Clamp the COMBINED offset: when the gesture fires from a deflected
        # head, the fading blend residual and the keyframe stroke can point the
        # same way on the driven axis and their sum would exceed the neck's
        # safe amplitude even though each part alone is within it.
        lim = self._neck_amplitude
        pitch = self.neck_pitch_center() + round(max(-lim, min(lim, offset["neck_pitch1"])))
        # ...and clamp the absolute pitch to the servo-neutral safety band too:
        # the rest trim shifts the working center, not the mechanical limits.
        self._neck["neck_pitch1"] = max(self.NEUTRAL - lim, min(self.NEUTRAL + lim, pitch))
        self._neck["neck_yaw"] = self.NEUTRAL + round(max(-lim, min(lim, offset["neck_yaw"])))
        # Zero the look target so IDLE after the gesture holds the front-facing
        # pose instead of pulling back toward a stale look() direction.
        self._neck_target_pitch = 0.0
        self._neck_target_yaw = 0.0

    def _apply_nod(self) -> None:
        """Nod "yes": the chin dips and returns, twice, second dip smaller."""
        self._apply_neck_gesture(self._build_nod_timeline(), "neck_pitch1")

    def _apply_head_shake(self) -> None:
        """Shake "no": quick sideways swings that taper off, ending front."""
        self._apply_neck_gesture(self._build_shake_timeline(), "neck_yaw")

    # ================================================================
    # NECK
    # ================================================================

    def _apply_neck(self) -> None:
        """Smoothly move neck toward target position."""
        center = self.NEUTRAL
        amp = self._neck_amplitude
        # Scale the per-cycle step by dt so the neck's approach speed is
        # wall-clock-true at any control rate (identical to the historical
        # 15 ticks/frame at the 60 fps default).
        step = max(1, round(self._neck_step * self.dt * 60.0))

        # Pitch — look targets ride on the resting trim (the trimmed center is
        # what "front" means for the head), but never leave the servo-neutral
        # safety band: the trim shifts the working center, not the mechanical
        # limits, so trim + full deflection must not stack past NEUTRAL±amp.
        target_p = self.neck_pitch_center() + int(self._neck_target_pitch * amp)
        target_p = max(center - amp, min(center + amp, target_p))
        current_p = self._neck["neck_pitch1"]
        if abs(current_p - target_p) <= step:
            self._neck["neck_pitch1"] = target_p
        elif current_p < target_p:
            self._neck["neck_pitch1"] += step
        else:
            self._neck["neck_pitch1"] -= step

        # Yaw
        target_y = center + int(self._neck_target_yaw * amp)
        current_y = self._neck["neck_yaw"]
        if abs(current_y - target_y) <= step:
            self._neck["neck_yaw"] = target_y
        elif current_y < target_y:
            self._neck["neck_yaw"] += step
        else:
            self._neck["neck_yaw"] -= step
