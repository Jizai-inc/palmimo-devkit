"""Pilot session state: a single-writer control slot and the latest pilot input with a
deadman timer.

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

        Move/rotate/gesture collapse to inert (translation/rotation/gesture
        off) when there is no pilot or the deadman has expired -- but the
        last-known neck target is always preserved, since holding gaze is
        never unsafe the way un-commanded translation or a running gesture
        is. A playing gesture (`play()`) is reported only while its window
        (`GESTURE_PLAY_SECONDS`) has not yet elapsed -- there is no separate
        "expire" step; this simply stops reporting it once `_now()` passes
        `gesture_until`.

        Reads `self._state` into a local exactly once: a second read could
        observe a fresher `_PilotState` the writer replaced in between,
        pairing this call's `last_input_at` with a different call's `input`
        (or vice versa) -- see the class docstring.
        """
        state = self._state
        current = state.input
        if not self.pilot_present or self._deadman_expired(state):
            return PilotInput(neck_pitch=current.neck_pitch, neck_yaw=current.neck_yaw)
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
        return ControlStatus(
            pilot_present=self.pilot_present,
            motion=resolve_motion(effective.move, effective.rotate, effective.gesture),
        )
