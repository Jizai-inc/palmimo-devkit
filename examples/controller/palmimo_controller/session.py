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
from dataclasses import dataclass, field

from .control import resolve_motion


#: How long the pilot's connection may go without a control frame before the
#: robot loop treats it as lost and stops translation/rotation.
DEADMAN_TIMEOUT_S: float = 0.4

#: How long a played gesture (`PilotSession.play`) keeps overriding move/
#: rotate before falling back to whatever the pilot is currently steering.
#: `palmimo_sdk` does not expose a wave or dance motion's own playback
#: length: `Palmimo.wave()` plays once and then holds neutral (so a window
#: longer than the playthrough is harmless), while `Palmimo.dance()` loops
#: until deselected (so this window is what ends it). Replace this with SDK
#: introspection if a future release publishes per-motion play length.
GESTURE_PLAY_SECONDS: dict[str, float] = {"wave": 5.0, "dance": 5.0}

#: The pilot's own input drives the robot.
MODE_TELEOP = "teleop"
#: An AI mode was picked and its session is still connecting; the robot holds still.
MODE_AI_STARTING = "ai_starting"
#: An AI mode is running; only its motion intents drive the robot.
MODE_AI = "ai"

#: Longest an AI motion intent may run before the robot falls back to still.
#: A model that asks to walk "for a while" must not walk off a table.
AI_INTENT_MAX_SECONDS: float = 5.0


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
class _ModeState:
    """Who drives the robot. Replaced wholesale, like `_PilotState`."""

    mode: str = MODE_TELEOP
    #: The AI mode's id while `mode` is not teleop.
    ai_mode: str | None = None
    #: A plain-language message for the pilot (why AI stopped), shown until the next switch.
    notice: str | None = None
    ai_input: PilotInput = field(default_factory=PilotInput)
    ai_until: float | None = None


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
        self._mode = _ModeState(mode=MODE_AI_STARTING, ai_mode=name)
        return True

    def ai_started(self) -> None:
        """Mark the AI session as live. A no-op unless a switch is in progress."""
        current = self._mode
        if current.mode == MODE_AI_STARTING:
            self._mode = _ModeState(mode=MODE_AI, ai_mode=current.ai_mode)

    def end_ai(self, notice: str | None = None) -> None:
        """Return to teleop, dropping any AI motion intent, with an optional message for the pilot."""
        self._mode = _ModeState(notice=notice)

    def set_ai_intent(self, intent: PilotInput, seconds: float) -> bool:
        """Have the robot follow *intent* for up to *seconds* (capped at `AI_INTENT_MAX_SECONDS`).

        Returns:
            bool: `False` (ignored) unless an AI mode is running.
        """
        current = self._mode
        if current.mode != MODE_AI:
            return False
        duration = max(0.0, min(seconds, AI_INTENT_MAX_SECONDS))
        self._mode = _ModeState(mode=MODE_AI, ai_mode=current.ai_mode, ai_input=intent, ai_until=self._now() + duration)
        return True

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
        until that intent's time runs out.

        In teleop, move/rotate/gesture/neck all collapse to inert (translation,
        rotation, gesture off, neck centered) when there is no pilot or the
        deadman has expired. The neck used to hold its last-known target
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
            if mode.ai_until is None or self._now() >= mode.ai_until:
                return PilotInput()
            return mode.ai_input
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
