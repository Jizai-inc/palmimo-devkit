import time
from typing import Any, ClassVar

from lerobot.teleoperators.keyboard import KeyboardTeleop, KeyboardTeleopConfig
from lerobot.teleoperators.teleoperator import Teleoperator
from lerobot.utils.errors import DeviceNotConnectedError

from palmimo_sdk import Motion, MotionEngine, kinematics

from .config_palmimo import PalmimoTeleopConfig


class PalmimoTeleop(Teleoperator):
    """WASD+QE keyboard teleoperator for the Palmimo hexapod robot.

    Keyboard input is read through lerobot's native ``KeyboardTeleop`` (pynput,
    no pygame/display window). Leg motion is delegated to ``palmimo_sdk``'s
    ``MotionEngine``; this teleop maps held keys to a motion and to neck control.

    Controls (character keys; see ``config.teleop_keys``):
    - W/S: Forward / Backward    A/D: Strafe Left / Right
    - Q/Z: Rotate Left / Right   E: Dance
    - O/L: Neck pitch up/down    N/M: Neck yaw left/right
    - 1-6/7-0, R/F/V/B, T/Y/U/I/G/H/J/K: IK calibration / debug poses
    - ESC: Quit

    Leg Layout (top-down view, front = neck direction):
    - Left side:  Leg 1 (RL), Leg 2 (ML), Leg 3 (FL)
    - Right side: Leg 4 (RR), Leg 5 (MR), Leg 6 (FR)
    """

    config_class = PalmimoTeleopConfig
    name = "palmimo"

    # Static debug poses. Each key sets every leg to neutral yaw and offsets the
    # two pitch joints by (p1, p2) * _DEBUG_OFFSET. `group` limits the offset to
    # one tripod set (the other legs stay neutral); None offsets all six legs.
    _DEBUG_OFFSET = 200
    _DEBUG_POSES: ClassVar[dict[str, tuple[list[int] | None, int, int]]] = {
        "r": (MotionEngine.TRIPOD_A, 1, -1),
        "f": (MotionEngine.TRIPOD_A, -1, 1),
        "v": (MotionEngine.TRIPOD_B, 1, -1),
        "b": (MotionEngine.TRIPOD_B, -1, 1),
        "t": (None, 1, 0),
        "y": (None, -1, 0),
        "u": (None, 0, 1),
        "i": (None, 0, -1),
        "g": (None, 1, 1),
        "h": (None, 1, -1),
        "j": (None, -1, 1),
        "k": (None, -1, -1),
    }

    def __init__(self, config: PalmimoTeleopConfig):
        super().__init__(config)
        self.config = config

        self._is_connected = False

        # Keyboard input via lerobot's native KeyboardTeleop (pynput-based).
        # get_action() returns the set of currently-held key.char strings.
        self._keyboard = KeyboardTeleop(KeyboardTeleopConfig())

        # Leg identity (used for diagnostic logging).
        self._leg_config = {
            1: {"name": "RL", "side": "left"},  # Rear Left
            2: {"name": "ML", "side": "left"},  # Middle Left
            3: {"name": "FL", "side": "left"},  # Front Left
            4: {"name": "RR", "side": "right"},  # Rear Right
            5: {"name": "MR", "side": "right"},  # Middle Right
            6: {"name": "FR", "side": "right"},  # Front Right
        }

        # Dynamixel center position (shared source: palmimo_sdk.kinematics)
        self._neutral = kinematics.NEUTRAL

        # Current motor positions — start at neutral. Leg IK math lives in
        # palmimo_sdk.kinematics (the shared source of truth); this teleop only
        # writes the resulting ticks here for the calibration/debug keys.
        self._leg_positions = {}
        for leg_id in range(1, 7):
            self._leg_positions[f"leg_{leg_id}_yaw"] = self._neutral
            self._leg_positions[f"leg_{leg_id}_pitch1"] = self._neutral
            self._leg_positions[f"leg_{leg_id}_pitch2"] = self._neutral

        # Gait tuning — also passed to the MotionEngine below.
        self._gait_speed = 0.012
        self._step_length = 30.0  # Forward stride in mm (firmware uses 30)
        self._step_height = 30.0  # Swing-leg lift in mm (matches the engine default)

        # Leg motion (gait + IK) is delegated to the SDK's MotionEngine — the
        # hardware-validated source of truth. The engine owns gait phase and leg
        # position state; this teleop keeps neck control and the visualization.
        self._engine = MotionEngine(
            gait_speed=self._gait_speed,
            step_length=self._step_length,
            step_height=self._step_height,
        )
        # True when the frame's leg positions come from the engine (movement /
        # idle); False when a debug key writes a pose directly to _leg_positions.
        self._engine_driven = False
        # Previous frame's _engine_driven, to detect a debug-key -> engine
        # handoff and reseed the engine so it eases from the held pose.
        self._was_engine_driven = False

        # Current motion state
        self._current_motion = "IDLE"

        # Neck positions (raw Dynamixel values, center=2048).
        # neck_pitch2 (motor 20) is on the bus like every other joint, and the robot
        # lists it in action_features; a teleop action that omits it does not leave
        # it alone, it makes the action undescribable (lerobot-record builds frames
        # from the robot's feature names and KeyErrors on the gap). Only neck_pitch1
        # is steered, so neck_pitch2 is held at neutral -- the pose it already sat
        # in when nothing wrote to it.
        self._neck_positions = {
            "neck_pitch1": self._neutral,
            "neck_pitch2": self._neutral,
            "neck_yaw": self._neutral,
        }

        # Neck movement limits
        self._neck_amplitude = 300  # Max deviation from center
        self._neck_step = 15  # Step size per frame

        # Logging
        self._frame_count = 0
        self._log_interval = 30  # Print every N frames (~0.5s at 60fps)
        self._last_log_time = time.time()

    @property
    def action_features(self) -> dict[str, type]:
        """Define action features for Palmimo robot."""
        motors: dict[str, type] = {}
        for leg_id in range(1, 7):
            motors[f"leg_{leg_id}_yaw.pos"] = float
            motors[f"leg_{leg_id}_pitch1.pos"] = float
            motors[f"leg_{leg_id}_pitch2.pos"] = float
        motors["neck_pitch1.pos"] = float
        motors["neck_pitch2.pos"] = float
        motors["neck_yaw.pos"] = float
        return motors

    @property
    def feedback_features(self) -> dict[str, type]:
        return {}

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    def connect(self, calibrate: bool = True) -> None:
        """Start the keyboard listener (lerobot KeyboardTeleop, pynput-based)."""
        self._keyboard.connect()
        # KeyboardTeleop degrades silently in a headless session (no display /
        # pynput): it stays disconnected. Fail clearly here instead of letting the
        # first get_action() raise a misleading "ESC pressed" KeyboardInterrupt.
        if not self._keyboard.is_connected:
            raise DeviceNotConnectedError(
                "Keyboard listener unavailable — pynput needs a display (Linux) or "
                "Accessibility permission (macOS). Palmimo teleop needs an "
                "interactive session."
            )
        self._is_connected = True

        print("\n╔══════════════════════════════════════════╗")
        print("║   Palmimo Hexapod Teleop                   ║")
        print("╠══════════════════════════════════════════╣")
        print("║  W: Forward    S: Backward               ║")
        print("║  A: Strafe L   D: Strafe R                ║")
        print("║  Q: Rotate L   Z: Rotate R                ║")
        print("║  E: Dance                                 ║")
        print("╠══════════════════════════════════════════╣")
        print("║  1-6: IK lift (per leg)  7: IK lift ALL   ║")
        print("║  8: IK fwd ALL  9: IK bwd  0: IK lat+lift ║")
        print("╠══════════════════════════════════════════╣")
        print("║  O/L: Neck Pitch  N/M: Neck Yaw          ║")
        print("║  ESC: Quit                               ║")
        print("╚══════════════════════════════════════════╝")
        print("\n  IK Gait params:")
        print(f"    l1={kinematics.L1}mm l2={kinematics.L2}mm l3={kinematics.L3}mm")
        print(f"    step_length:      {self._step_length}mm")
        print(f"    step_height:      {self._step_height}mm")
        print(f"    gait_speed:       {self._gait_speed}")
        print(f"    neutral:          {self._neutral}")
        print(f"    tripod_A: {MotionEngine.TRIPOD_A}")
        print(f"    tripod_B: {MotionEngine.TRIPOD_B}")
        print()

    def disconnect(self) -> None:
        if self._is_connected:
            if self._keyboard.is_connected:
                self._keyboard.disconnect()
            self._is_connected = False

    @property
    def is_calibrated(self) -> bool:
        return True

    def calibrate(self) -> None:
        pass

    def configure(self) -> None:
        pass

    # ================================================================
    # INVERSE KINEMATICS (calibration / debug poses)
    # ================================================================

    def _set_leg_from_ik(self, leg_id: int, dx: float, dy: float, dz: float) -> None:
        """Write one leg's servo ticks from a foot displacement via shared IK.

        The IK math is owned by ``palmimo_sdk.kinematics``; this only stores the
        resulting ticks for the calibration keys.
        """
        self._leg_positions.update(kinematics.leg_servo_ticks(leg_id, dx, dy, dz))

    # ================================================================
    # MOTION COMMANDS — select a Motion; the MotionEngine computes the gait
    # ================================================================
    #
    # Each command sets the engine's motion (and display state) and marks the
    # frame engine-driven; get_action() then advances the engine and pulls the
    # leg positions. Direction signs live in the engine (the hardware-validated
    # source of truth), so forward/backward/left/right are correct by delegation.

    def _stop_motion(self) -> None:
        """Stop all motion and ease back to neutral (engine IDLE)."""
        self._current_motion = "IDLE"
        self._engine.motion = Motion.IDLE
        self._engine_driven = True

    def _move_forward(self) -> None:
        """Walk forward."""
        self._current_motion = "FORWARD"
        self._engine.motion = Motion.FORWARD
        self._engine_driven = True

    def _move_backward(self) -> None:
        """Walk backward."""
        self._current_motion = "BACKWARD"
        self._engine.motion = Motion.BACKWARD
        self._engine_driven = True

    def _move_left(self) -> None:
        """Strafe left."""
        self._current_motion = "STRAFE_LEFT"
        self._engine.motion = Motion.STRAFE_LEFT
        self._engine_driven = True

    def _move_right(self) -> None:
        """Strafe right."""
        self._current_motion = "STRAFE_RIGHT"
        self._engine.motion = Motion.STRAFE_RIGHT
        self._engine_driven = True

    def _do_dance(self) -> None:
        """Dance (body sway)."""
        self._current_motion = "DANCE"
        self._engine.motion = Motion.DANCE
        self._engine_driven = True

    def _rotate_left(self) -> None:
        """Rotate body left (CCW from top)."""
        self._current_motion = "ROTATE_LEFT"
        self._engine.motion = Motion.ROTATE_LEFT
        self._engine_driven = True

    def _rotate_right(self) -> None:
        """Rotate body right (CW from top)."""
        self._current_motion = "ROTATE_RIGHT"
        self._engine.motion = Motion.ROTATE_RIGHT
        self._engine_driven = True

    def _ik_test_leg(self, leg_id: int, dx: float, dy: float, dz: float, desc: str) -> None:
        """Test IK on a single leg, others stay neutral."""
        self._current_motion = "IK_TEST"
        n = self._neutral

        # Set all legs to neutral first
        for lid in range(1, 7):
            if lid == leg_id:
                self._set_leg_from_ik(lid, dx, dy, dz)
            else:
                self._leg_positions[f"leg_{lid}_yaw"] = n
                self._leg_positions[f"leg_{lid}_pitch1"] = n
                self._leg_positions[f"leg_{lid}_pitch2"] = n

        # Print IK details for this leg (only once per second)
        if self._frame_count % 60 == 0:
            yaw_deg, p1_deg, p2_deg = kinematics.leg_ik(leg_id, dx, dy, dz)
            tpd = kinematics.TICK_PER_DEG
            print(
                f"  IK Leg {leg_id}: ({dx},{dy},{dz})mm -> "
                f"yaw={yaw_deg:.1f}° p1={p1_deg:.1f}° p2={p2_deg:.1f}° | "
                f"ticks: yaw={int(yaw_deg * tpd)} p1={int(p1_deg * tpd)} p2={int(p2_deg * tpd)} | "
                f"sent: yaw={self._leg_positions[f'leg_{leg_id}_yaw'] - n} "
                f"p1={self._leg_positions[f'leg_{leg_id}_pitch1'] - n} "
                f"p2={self._leg_positions[f'leg_{leg_id}_pitch2'] - n}"
            )

    def _ik_test_all(self, dx: float, dy: float, dz: float, desc: str) -> None:
        """Test IK on all legs with same displacement."""
        self._current_motion = "IK_TEST"

        for lid in range(1, 7):
            self._set_leg_from_ik(lid, dx, dy, dz)

        # Print IK details (only once per second)
        if self._frame_count % 60 == 0:
            n = self._neutral
            print(f"  IK ALL: ({dx},{dy},{dz})mm")
            for lid in range(1, 7):
                yaw_deg, p1_deg, p2_deg = kinematics.leg_ik(lid, dx, dy, dz)
                print(
                    f"    Leg {lid}: yaw={yaw_deg:>6.1f}° p1={p1_deg:>6.1f}° p2={p2_deg:>6.1f}° | "
                    f"sent: yaw={self._leg_positions[f'leg_{lid}_yaw'] - n:>5} "
                    f"p1={self._leg_positions[f'leg_{lid}_pitch1'] - n:>5} "
                    f"p2={self._leg_positions[f'leg_{lid}_pitch2'] - n:>5}"
                )

    # ================================================================
    # NECK CONTROL
    # ================================================================

    def _update_neck_from_keys(self, pressed: set[str]) -> None:
        """Update neck position from held character keys (see config.teleop_keys)."""
        nk = self.config.teleop_keys
        center = self._neutral
        min_pos = center - self._neck_amplitude
        max_pos = center + self._neck_amplitude

        # Pitch control (up/down)
        if nk["neck_pitch_up"] in pressed:
            self._neck_positions["neck_pitch1"] = min(self._neck_positions["neck_pitch1"] + self._neck_step, max_pos)
        elif nk["neck_pitch_down"] in pressed:
            self._neck_positions["neck_pitch1"] = max(self._neck_positions["neck_pitch1"] - self._neck_step, min_pos)
        else:
            current = self._neck_positions["neck_pitch1"]
            if abs(current - center) < self._neck_step:
                self._neck_positions["neck_pitch1"] = center
            elif current > center:
                self._neck_positions["neck_pitch1"] -= self._neck_step
            else:
                self._neck_positions["neck_pitch1"] += self._neck_step

        # Yaw control (left/right)
        if nk["neck_yaw_left"] in pressed:
            self._neck_positions["neck_yaw"] = min(self._neck_positions["neck_yaw"] + self._neck_step, max_pos)
        elif nk["neck_yaw_right"] in pressed:
            self._neck_positions["neck_yaw"] = max(self._neck_positions["neck_yaw"] - self._neck_step, min_pos)
        else:
            current = self._neck_positions["neck_yaw"]
            if abs(current - center) < self._neck_step:
                self._neck_positions["neck_yaw"] = center
            elif current > center:
                self._neck_positions["neck_yaw"] -= self._neck_step
            else:
                self._neck_positions["neck_yaw"] += self._neck_step

    # ================================================================
    # INPUT HANDLING
    # ================================================================

    def _apply_debug_pose(self, group: list[int] | None, p1: int, p2: int) -> None:
        """Write a static debug pose: neutral yaw on every leg, the two pitch joints
        offset by (p1, p2) * _DEBUG_OFFSET. When *group* is given only its legs are
        offset (the rest stay neutral); when None all six legs are offset.
        """
        self._current_motion = "TEST"
        n = self._neutral
        off = self._DEBUG_OFFSET
        for lid in range(1, 7):
            self._leg_positions[f"leg_{lid}_yaw"] = n
            if group is None or lid in group:
                self._leg_positions[f"leg_{lid}_pitch1"] = n + p1 * off
                self._leg_positions[f"leg_{lid}_pitch2"] = n + p2 * off
            else:
                self._leg_positions[f"leg_{lid}_pitch1"] = n
                self._leg_positions[f"leg_{lid}_pitch2"] = n

    def _update_motion_from_keys(self, pressed: set[str]) -> None:
        """Update robot motion from the set of currently-held character keys.

        Movement (see config.teleop_keys):
          W: Forward   S: Backward
          A: Strafe Left  D: Strafe Right
          Q: Rotate Left  Z: Rotate Right
          E: Dance
        Test keys (fixed character keys):
          R/F/V/B: Tripod lift tests
          T/Y/U/I/G/H/J/K: Individual motor tests
          1-6: IK lift per leg  7-0: IK direction tests
        """
        mk = self.config.teleop_keys
        # Debug-key branches below write _leg_positions directly. Movement and
        # idle set _engine_driven=True; reset to False here so a debug pose
        # (which leaves it False) keeps get_action from advancing the engine.
        self._engine_driven = False

        # Movement keys are mutually exclusive (if/elif): the MotionEngine has one
        # active gait at a time, so compound moves (e.g. W+A) resolve to the first
        # match. Neck keys are evaluated separately below and stay independent.
        if mk["forward"] in pressed:
            self._move_forward()
        elif mk["backward"] in pressed:
            self._move_backward()
        elif mk["strafe_left"] in pressed:
            self._move_left()
        elif mk["strafe_right"] in pressed:
            self._move_right()
        elif mk["rotate_left"] in pressed:
            self._rotate_left()
        elif mk["rotate_right"] in pressed:
            self._rotate_right()
        elif mk["dance"] in pressed:
            self._do_dance()
        # Static debug poses (see _DEBUG_POSES): write a fixed leg pose directly.
        elif (pose := next((k for k in self._DEBUG_POSES if k in pressed), None)) is not None:
            self._apply_debug_pose(*self._DEBUG_POSES[pose])
        # ---- IK CALIBRATION TESTS ----
        # Number keys 1-6: IK lift test on individual leg (dz=+40mm)
        # 7: IK lift ALL legs (dz=+40mm)
        # 8: IK forward ALL legs (dx=+30mm)
        # 9: IK backward+lift ALL legs (dx=-30, dz=+40)
        # 0: IK lateral+lift ALL legs (dy=+30, dz=+40)
        elif "1" in pressed:
            self._ik_test_leg(1, 0, 0, 40, "IK Lift Leg1(RL)")
        elif "2" in pressed:
            self._ik_test_leg(2, 0, 0, 40, "IK Lift Leg2(ML)")
        elif "3" in pressed:
            self._ik_test_leg(3, 0, 0, 40, "IK Lift Leg3(FL)")
        elif "4" in pressed:
            self._ik_test_leg(4, 0, 0, 40, "IK Lift Leg4(RR)")
        elif "5" in pressed:
            self._ik_test_leg(5, 0, 0, 40, "IK Lift Leg5(MR)")
        elif "6" in pressed:
            self._ik_test_leg(6, 0, 0, 40, "IK Lift Leg6(FR)")
        elif "7" in pressed:
            self._ik_test_all(0, 0, 40, "IK Lift ALL dz=+40")
        elif "8" in pressed:
            self._ik_test_all(30, 0, 40, "IK +X+Lift dx=+30 dz=+40")
        elif "9" in pressed:
            self._ik_test_all(-30, 0, 40, "IK -X+Lift dx=-30 dz=+40")
        elif "0" in pressed:
            self._ik_test_all(0, 30, 40, "IK +Y+Lift dy=+30 dz=+40")
        else:
            self._stop_motion()

        # Update neck position (independent of legs)
        self._update_neck_from_keys(pressed)

    # ================================================================
    # MAIN ACTION LOOP
    # ================================================================

    def get_action(self) -> dict[str, Any]:
        """Get current action based on keyboard input."""
        if not self.is_connected:
            action = {}
            center = self._neutral
            for leg_id in range(1, 7):
                action[f"leg_{leg_id}_yaw.pos"] = center
                action[f"leg_{leg_id}_pitch1.pos"] = center
                action[f"leg_{leg_id}_pitch2.pos"] = center
            action["neck_pitch1.pos"] = center
            action["neck_pitch2.pos"] = center
            action["neck_yaw.pos"] = center
            return action

        # KeyboardTeleop disconnects itself when ESC is released; treat that as
        # the quit signal.
        if not self._keyboard.is_connected:
            self._is_connected = False
            raise KeyboardInterrupt("User pressed ESC to quit")

        # Currently-held character keys, e.g. {"w", "o"}.
        pressed = set(self._keyboard.get_action().keys())

        # Update motion
        self._update_motion_from_keys(pressed)

        # Advance the engine and pull leg positions, unless a debug key wrote a
        # pose directly this frame. Neck stays under this teleop's control.
        if self._engine_driven:
            # On handoff from a direct debug-key pose, seed the engine with the
            # held pose so step() eases from it instead of snapping from stale
            # internal state.
            if not self._was_engine_driven:
                self._engine.set_leg_positions(self._leg_positions)
            engine_pos = self._engine.step()
            for key in self._leg_positions:
                self._leg_positions[key] = engine_pos[key]
        self._was_engine_driven = self._engine_driven

        # Build action dictionary
        action = {}
        for leg_id in range(1, 7):
            action[f"leg_{leg_id}_yaw.pos"] = self._leg_positions[f"leg_{leg_id}_yaw"]
            action[f"leg_{leg_id}_pitch1.pos"] = self._leg_positions[f"leg_{leg_id}_pitch1"]
            action[f"leg_{leg_id}_pitch2.pos"] = self._leg_positions[f"leg_{leg_id}_pitch2"]

        # Neck positions
        action["neck_pitch1.pos"] = self._neck_positions["neck_pitch1"]
        action["neck_pitch2.pos"] = self._neck_positions["neck_pitch2"]
        action["neck_yaw.pos"] = self._neck_positions["neck_yaw"]

        # === DIAGNOSTIC LOGGING ===
        self._frame_count += 1
        if self._frame_count % self._log_interval == 0:
            now = time.time()
            dt = now - self._last_log_time
            fps = self._log_interval / dt if dt > 0 else 0
            self._last_log_time = now

            n = self._neutral
            print(
                f"\n[Frame {self._frame_count}] FPS={fps:.1f} | Motion={self._current_motion} | Phase={self._engine.gait_phase:.3f}"
            )
            print(f"  {'Leg':>8} | {'Yaw':>6} {'Δ':>6} | {'Pitch1':>6} {'Δ':>6} | {'Pitch2':>6} {'Δ':>6} | Side")
            print(f"  {'─' * 8}-+-{'─' * 13}-+-{'─' * 13}-+-{'─' * 13}-+------")
            for leg_id in range(1, 7):
                cfg = self._leg_config[leg_id]
                name = cfg["name"]
                side = cfg["side"]
                group = "A" if leg_id in MotionEngine.TRIPOD_A else "B"

                yaw = action[f"leg_{leg_id}_yaw.pos"]
                p1 = action[f"leg_{leg_id}_pitch1.pos"]
                p2 = action[f"leg_{leg_id}_pitch2.pos"]

                # Show delta from neutral (2048)
                dy = yaw - n
                dp1 = p1 - n
                dp2 = p2 - n

                # Flag if pitch is not at neutral (shouldn't be for yaw-only gait)
                p1_flag = " !!!" if dp1 != 0 and self._current_motion == "FORWARD" else ""

                print(
                    f"  {leg_id}({name}){group} | {yaw:>6} {dy:>+6} | {p1:>6} {dp1:>+6}{p1_flag} | {p2:>6} {dp2:>+6} | {side}"
                )

            # Summary: check symmetry
            if self._current_motion == "FORWARD":
                a_yaws = [action[f"leg_{l}_yaw.pos"] - n for l in MotionEngine.TRIPOD_A]
                b_yaws = [action[f"leg_{l}_yaw.pos"] - n for l in MotionEngine.TRIPOD_B]
                print(f"  Tripod A yaw deltas: {a_yaws}")
                print(f"  Tripod B yaw deltas: {b_yaws}")
                print(f"  A+B sum: {sum(a_yaws) + sum(b_yaws)} (should be ~0 for straight)")

            # Neck
            ny = action["neck_yaw.pos"]
            np1 = action["neck_pitch1.pos"]
            print(f"  Neck: yaw={ny} ({ny - n:>+5}) | p1={np1} ({np1 - n:>+5})")

        return action

    def send_feedback(self, feedback: dict[str, Any]) -> None:
        pass
