"""Pilot session state: a single-writer control slot, the latest pilot input with a
deadman timer, and which mode (manual teleop or an AI mode) drives the robot.

Pure and clock-injected (no `time.sleep`, no I/O) so `robot_loop.RobotLoop` and
`server.py` share one source of truth for "what should the robot be doing
right now" without either owning a background timer, and so tests exercise the
400ms deadman window without actually waiting on it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field, replace

from .control import resolve_motion


#: How long the pilot's connection may go without a control frame before the
#: robot loop treats it as lost and stops translation/rotation.
DEADMAN_TIMEOUT_S: float = 0.4

#: How long a played gesture (`PilotSession.play`) keeps overriding move/
#: rotate before falling back to whatever the pilot is currently steering.
#: `palmimo_sdk` does not expose most motions' own playback length. The
#: one-shots (wave, nod ~1.5 s, clap ~2 s, bow ~1.7 s) play once and then hold
#: neutral, so a window longer than the playthrough is harmless; dance,
#: pushup and body_tilt loop until deselected, so this window is what ends
#: them. Replace this with SDK introspection if a future release publishes
#: per-motion play length.
GESTURE_PLAY_SECONDS: dict[str, float] = {
    "wave": 5.0,
    "dance": 5.0,
    "nod": 5.0,
    "clap": 5.0,
    "pushup": 5.0,
    "body_tilt": 5.0,
    "bow": 5.0,
}

#: The pilot's own input drives the robot.
MODE_TELEOP = "teleop"
#: An AI mode was picked and its session is still connecting; the robot holds still.
MODE_AI_STARTING = "ai_starting"
#: An AI mode is running; only its motion intents drive the robot.
MODE_AI = "ai"

#: Longest an AI motion intent may run before the robot falls back to still.
#: A model that asks to walk "for a while" must not walk off a table.
AI_INTENT_MAX_SECONDS: float = 5.0

#: How long the nod that greets a live AI session plays before the robot holds still.
#: Covers one nod (~1.5 s); the AI's first motion intent replaces it early.
AI_START_NOD_SECONDS: float = 2.0

#: Face expressions the robot cycles through while an AI session connects,
#: so a visitor sees it "thinking" instead of a frozen face.
STARTING_FACES: tuple[str, ...] = ("THINKING", "IDEA", "SMILE")
#: How long each `STARTING_FACES` expression shows before the next.
STARTING_FACE_SECONDS: float = 0.7
#: Face shown when the AI session goes live, then the firmware returns to IDLE.
LIVE_FACE: str = "HAPPY"
#: Face shown when an AI session ends with a notice (it failed or was cut off).
FAILED_FACE: str = "SAD"
#: How long `LIVE_FACE` / `FAILED_FACE` hold before the firmware returns to IDLE.
CUE_FACE_HOLD_MS: int = 2000


@dataclass(frozen=True)
class PilotInput:
    """One resolved pilot command: what `robot_loop.RobotLoop` should apply this tick."""

    move: str | None = None
    rotate: int = 0
    gesture: str | None = None
    neck_pitch: float = 0.0
    neck_yaw: float = 0.0


@dataclass(frozen=True)
class ControlStatus:
    """The `@2Hz` broadcast payload's control-related fields."""

    pilot_present: bool
    motion: str | None
    mode: str = MODE_TELEOP
    ai_mode: str | None = None
    notice: str | None = None


@dataclass(frozen=True)
class FaceCue:
    """What the robot's face should show right now.

    `robot_loop.RobotLoop` applies a cue only when *key* changes, so one
    expression is sent once rather than on every control cycle.
    """

    key: str
    #: Expression name, or `None` for the neutral IDLE face.
    expression: str | None = None
    #: `0` holds until the next cue; above `0` the firmware returns to IDLE after it.
    hold_ms: int = 0


#: The neutral face: teleop with nothing to report.
IDLE_FACE_CUE = FaceCue(key="idle")


@dataclass(frozen=True)
class _ModeState:
    """Who drives the robot. Replaced wholesale, like `_PilotState`."""

    mode: str = MODE_TELEOP
    #: The AI mode's id while `mode` is not teleop.
    ai_mode: str | None = None
    #: A plain-language message for the pilot (why AI stopped), shown until the next switch.
    notice: str | None = None
    ai_input: PilotInput = field(default_factory=PilotInput)
    ai_until: float | None = None
    #: When this mode state began, for the time-based face cue. `None` before any switch.
    since: float | None = None
    #: Neck target `(pitch, yaw)` a reflex glance holds while no AI intent is running.
    gaze: tuple[float, float] | None = None


@dataclass
class _PilotState:
    input: PilotInput = field(default_factory=PilotInput)
    last_input_at: float | None = None
    #: Gesture currently playing (`play()`), or `None`. Held here rather than
    #: on `input` because it is not part of the pilot's per-frame move/rotate
    #: input -- it is set by an independent `{"play": ...}` message and, per
    #: the class docstring's replace-wholesale invariant, must live in the
    #: same object as `last_input_at` so a reader thread never pairs a
    #: gesture with a mismatched deadman clock.
    playing_gesture: str | None = None
    gesture_until: float | None = None


class PilotSession:
    """Owns the pilot slot and the latest pilot input.

    One instance is shared between the WebSocket handler (the single asyncio
    event loop, the only writer) and `robot_loop.RobotLoop` (a second, reader-
    only thread). There is no lock: safety instead rests on `_PilotState`
    always being replaced wholesale, never mutated in place, combined with
    the GIL -- a reader on the other thread either observes the old
    `_PilotState` object or the new one, never a mix of old and new fields.
    That guarantee only holds if a reader takes exactly one reference to
    `self._state`; see `effective_input()`.
    """

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._pilot_id: Hashable | None = None
        self._state = _PilotState()
        self._mode = _ModeState()

    @property
    def mode(self) -> str:
        """`MODE_TELEOP`, `MODE_AI_STARTING`, or `MODE_AI`."""
        return self._mode.mode

    @property
    def ai_mode(self) -> str | None:
        """The running (or starting) AI mode's id, or `None` in teleop."""
        return self._mode.ai_mode

    def begin_ai(self, client_id: Hashable, name: str) -> bool:
        """Start switching to AI mode *name*.

        Only allowed from teleop, so a second tap while a switch is still
        resolving is ignored rather than starting a second AI session.

        Returns:
            bool: `False` (nothing changed) if *client_id* is not the pilot
                or the session is not in teleop.
        """
        if self._pilot_id != client_id or self._mode.mode != MODE_TELEOP:
            return False
        self._mode = _ModeState(mode=MODE_AI_STARTING, ai_mode=name, since=self._now())
        return True

    def ai_started(self) -> None:
        """Mark the AI session as live and greet it with one nod. A no-op unless a switch is in progress.

        The nod goes through the AI intent path, so it plays through
        `Palmimo.step()` like any gesture, and the AI's first motion replaces it.
        """
        current = self._mode
        if current.mode == MODE_AI_STARTING:
            now = self._now()
            self._mode = _ModeState(
                mode=MODE_AI,
                ai_mode=current.ai_mode,
                ai_input=PilotInput(gesture="nod"),
                ai_until=now + AI_START_NOD_SECONDS,
                since=now,
            )

    def end_ai(self, notice: str | None = None) -> None:
        """Return to teleop, dropping any AI motion intent, with an optional message for the pilot."""
        self._mode = _ModeState(notice=notice, since=self._now())

    def set_ai_intent(self, intent: PilotInput, seconds: float) -> bool:
        """Have the robot follow *intent* for up to *seconds* (capped at `AI_INTENT_MAX_SECONDS`).

        Returns:
            bool: `False` (ignored) unless an AI mode is running.
        """
        current = self._mode
        if current.mode != MODE_AI:
            return False
        duration = max(0.0, min(seconds, AI_INTENT_MAX_SECONDS))
        self._mode = _ModeState(
            mode=MODE_AI,
            ai_mode=current.ai_mode,
            ai_input=intent,
            ai_until=self._now() + duration,
            since=current.since,
        )
        return True

    @property
    def ai_busy(self) -> bool:
        """Whether an AI motion intent (a tool call, the go-live nod, a reflex wave) is still running."""
        mode = self._mode
        return mode.mode == MODE_AI and mode.ai_until is not None and self._now() < mode.ai_until

    def set_gaze(self, pitch: float, yaw: float) -> bool:
        """Point the neck at *pitch*/*yaw* while no AI intent runs (a reflex glance).

        An AI intent always wins over the gaze, so a glance never overrides
        something the model asked for.

        Returns:
            bool: `False` (ignored) unless an AI mode is running.
        """
        current = self._mode
        if current.mode != MODE_AI:
            return False
        self._mode = replace(current, gaze=(pitch, yaw))
        return True

    def clear_gaze(self) -> None:
        """End a reflex glance; the neck recenters unless an AI intent drives it."""
        current = self._mode
        if current.gaze is not None:
            self._mode = replace(current, gaze=None)

    def face_cue(self) -> FaceCue:
        """What the face should show for the current mode.

        While an AI session connects, the face cycles through `STARTING_FACES`;
        each step has its own key, so the robot loop sends every step once.
        A live session shows `LIVE_FACE`, a session that ended with a notice
        shows `FAILED_FACE`, and plain teleop is the neutral IDLE face.
        """
        mode = self._mode
        if mode.mode == MODE_AI_STARTING:
            elapsed = 0.0 if mode.since is None else max(0.0, self._now() - mode.since)
            step = int(elapsed / STARTING_FACE_SECONDS)
            return FaceCue(key=f"starting:{step}", expression=STARTING_FACES[step % len(STARTING_FACES)])
        # `since` keeps the key distinct per switch: two failed starts in a row
        # must each show the face, not leave the second one unsent.
        if mode.mode == MODE_AI:
            return FaceCue(key=f"live:{mode.since}", expression=LIVE_FACE, hold_ms=CUE_FACE_HOLD_MS)
        if mode.notice is not None:
            return FaceCue(key=f"failed:{mode.since}", expression=FAILED_FACE, hold_ms=CUE_FACE_HOLD_MS)
        return IDLE_FACE_CUE

    @property
    def pilot_present(self) -> bool:
        """Whether a pilot currently holds the control slot."""
        return self._pilot_id is not None

    def acquire_pilot(self, client_id: Hashable) -> bool:
        """Claim the pilot slot for *client_id*.

        Returns:
            bool: `True` if the slot was free or already held by *client_id*
                (idempotent); `False` if another pilot holds it.
        """
        if self._pilot_id is not None and self._pilot_id != client_id:
            return False
        self._pilot_id = client_id
        return True

    def release_pilot(self, client_id: Hashable) -> None:
        """Free the pilot slot and reset all pilot state, if *client_id* holds it.

        A no-op for any other *client_id* (including a viewer, or a pilot
        that already lost the slot) -- releasing is not a way to steal it.
        """
        if self._pilot_id != client_id:
            return
        self._pilot_id = None
        self._state = _PilotState()

    def update_pilot_input(
        self,
        client_id: Hashable,
        *,
        move: str | None,
        rotate: int,
        neck_pitch: float,
        neck_yaw: float,
    ) -> bool:
        """Record one validated pilot frame and refresh the deadman clock.

        A non-idle *move* or *rotate* cancels a gesture already playing
        (`play()`) -- the pilot's steering intent takes priority over
        letting a played gesture run out its window.

        Args:
            client_id (Hashable): Must match the current pilot holder.
            move (str, optional): Already validated by the caller.
            rotate (int): Already validated by the caller.
            neck_pitch (float): Already clamped by the caller.
            neck_yaw (float): Already clamped by the caller.

        Returns:
            bool: `False` (frame dropped, no state changed) if *client_id* is
                not the current pilot.
        """
        if self._pilot_id != client_id:
            return False
        previous = self._state
        steering = move is not None or rotate != 0
        self._state = _PilotState(
            input=PilotInput(move=move, rotate=rotate, neck_pitch=neck_pitch, neck_yaw=neck_yaw),
            last_input_at=self._now(),
            playing_gesture=None if steering else previous.playing_gesture,
            gesture_until=None if steering else previous.gesture_until,
        )
        return True

    def play(self, client_id: Hashable, name: str) -> bool:
        """Play gesture *name* for the current pilot; toggle it off if it is already playing.

        Re-playing the gesture that is currently active stops it early
        rather than restarting its window (tap-to-toggle, matching the
        pilot page's click handler); playing a different gesture replaces
        whichever one is running.

        Args:
            client_id (Hashable): Must match the current pilot holder.
            name (str): A key of `GESTURE_PLAY_SECONDS`; already validated
                by the caller.

        Returns:
            bool: `False` (dropped, no state changed) if *client_id* is not
                the current pilot.
        """
        if self._pilot_id != client_id:
            return False
        state = self._state
        now = self._now()
        active = state.gesture_until is not None and now < state.gesture_until
        if active and state.playing_gesture == name:
            playing_gesture, gesture_until = None, None
        else:
            playing_gesture, gesture_until = name, now + GESTURE_PLAY_SECONDS[name]
        self._state = _PilotState(
            input=state.input,
            last_input_at=state.last_input_at,
            playing_gesture=playing_gesture,
            gesture_until=gesture_until,
        )
        return True

    def _deadman_expired(self, state: _PilotState) -> bool:
        last = state.last_input_at
        return last is None or (self._now() - last) > DEADMAN_TIMEOUT_S

    def effective_input(self) -> PilotInput:
        """Resolve what `robot_loop.RobotLoop` should apply right now.

        In an AI mode the pilot's input is ignored: the robot holds still
        while the AI session connects, then follows the AI's latest intent
        until that intent's time runs out. Between intents, a reflex glance's
        gaze (`set_gaze`) points the neck; otherwise the robot holds still.

        In teleop, move/rotate/gesture/neck all collapse to inert
        (translation, rotation, gesture off, neck centered) when there is no pilot or the deadman has expired. The neck used to hold its last-known target
        through a deadman expiry on the theory that holding gaze is never
        unsafe; a physical robot proved otherwise -- holding the neck at an
        extreme (e.g. fully up) is a known cause of neck-servo overheating,
        so an unattended neck must recenter, not freeze wherever the pilot
        last left it. A playing gesture (`play()`) is reported only while
        its window (`GESTURE_PLAY_SECONDS`) has not yet elapsed -- there is
        no separate "expire" step; this simply stops reporting it once
        `_now()` passes `gesture_until`.

        Reads `self._state` into a local exactly once: a second read could
        observe a fresher `_PilotState` the writer replaced in between,
        pairing this call's `last_input_at` with a different call's `input`
        (or vice versa) -- see the class docstring.
        """
        mode = self._mode
        if mode.mode == MODE_AI_STARTING:
            return PilotInput()
        if mode.mode == MODE_AI:
            if mode.ai_until is not None and self._now() < mode.ai_until:
                return mode.ai_input
            if mode.gaze is not None:
                return PilotInput(neck_pitch=mode.gaze[0], neck_yaw=mode.gaze[1])
            return PilotInput()
        state = self._state
        current = state.input
        if not self.pilot_present or self._deadman_expired(state):
            return PilotInput()
        gesture = None
        if state.gesture_until is not None and self._now() < state.gesture_until:
            gesture = state.playing_gesture
        return PilotInput(
            move=current.move,
            rotate=current.rotate,
            gesture=gesture,
            neck_pitch=current.neck_pitch,
            neck_yaw=current.neck_yaw,
        )

    def status(self) -> ControlStatus:
        """The current `@2Hz` broadcast status, derived from `effective_input`."""
        effective = self.effective_input()
        mode = self._mode
        return ControlStatus(
            pilot_present=self.pilot_present,
            motion=resolve_motion(effective.move, effective.rotate, effective.gesture),
            mode=mode.mode,
            ai_mode=mode.ai_mode,
            notice=mode.notice,
        )
