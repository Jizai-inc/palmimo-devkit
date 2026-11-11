"""Idle behaviour for a live AI session: small signs of life between replies.

Without this the robot freezes whenever the model is quiet, which reads as a
tool waiting for commands rather than a pet. After `IDLE_AFTER_S` with no AI
intent and no reflex glance, `IdleJudge` picks a small behaviour now and then:
a slow look around (neck only), a body tilt, a stretch, or a short face.

Idle is the lowest priority in `session.PilotSession.effective_input()`: any
AI intent or reflex glance drops it at once, and ending the AI session (the
pilot leaving included) drops it with everything else. Idle never walks or
turns, and its neck targets stay small and short, because holding the neck
off-center heats the neck servo.

Runs as one more task in the AI session's task group, like `reflexes.Reflexes`,
so it writes the session from the same single writer as everything else.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable

from ..session import GESTURE_PLAY_SECONDS, MODE_AI, PilotInput, PilotSession
from ..telemetry import Telemetry


_LOG = logging.getLogger(__name__)

#: Quiet time (no AI intent, no glance) before the first idle behaviour.
IDLE_AFTER_S: float = 4.0
#: Range of the random pause between the end of one idle behaviour and the next.
IDLE_PAUSE_S: tuple[float, float] = (4.0, 10.0)
#: Largest |pitch| and |yaw| an idle look may use, so the neck servo stays cool.
IDLE_NECK_MAX: float = 0.3
#: How long one idle look holds before the neck recenters.
IDLE_LOOK_SECONDS: float = 2.0
#: How long an idle body tilt plays. The tilt loops until deselected, so this ends it.
IDLE_TILT_SECONDS: float = 2.5
#: How long an idle face shows before the neutral face returns.
IDLE_FACE_SECONDS: float = 1.5
#: Expressions an idle face picks from: calm ones, since nothing prompted them.
IDLE_FACES: tuple[str, ...] = ("SMILE", "THINKING")
#: The behaviours idle picks from. None walks or turns.
IDLE_BEHAVIOURS: tuple[str, ...] = ("look_around", "body_tilt", "stretch", "face")
#: How often the runner asks the judge whether to act.
IDLE_PERIOD_S: float = 0.2


class IdleJudge:
    """Decides when and what to do while the AI session is quiet. Pure: no I/O, clock and RNG passed in."""

    def __init__(
        self, session: PilotSession, *, rng: random.Random | None = None, telemetry: Telemetry | None = None
    ) -> None:
        self._session = session
        self._rng = rng if rng is not None else random.Random()
        self._telemetry = telemetry
        self._quiet_since: float | None = None
        self._next_at: float | None = None

    def update(self, now: float) -> None:
        """Act on the session's state at time *now*: wait, start a behaviour, or let one run."""
        session = self._session
        if session.mode != MODE_AI:
            self._quiet_since = None
            self._next_at = None
            return
        if session.ai_busy or session.gazing:
            # Something else drives the robot; the quiet time starts over once it stops.
            self._quiet_since = now
            self._next_at = None
            return
        if self._quiet_since is None:
            self._quiet_since = now
        if now - self._quiet_since < IDLE_AFTER_S or session.idle_active:
            return
        if self._next_at is None:
            self._next_at = now
        if now < self._next_at:
            return
        behaviour = self._rng.choice(IDLE_BEHAVIOURS)
        duration = self._play(behaviour)
        self._next_at = now + duration + self._rng.uniform(*IDLE_PAUSE_S)

    def stop(self) -> None:
        """End any idle behaviour, recentering the neck."""
        self._session.clear_idle()

    def _play(self, behaviour: str) -> float:
        """Start *behaviour* and return how long it runs. A refused write just skips this turn."""
        if behaviour == "look_around":
            pitch = self._rng.uniform(-IDLE_NECK_MAX, IDLE_NECK_MAX)
            yaw = self._rng.uniform(-IDLE_NECK_MAX, IDLE_NECK_MAX)
            ok = self._session.set_idle(PilotInput(neck_pitch=pitch, neck_yaw=yaw), IDLE_LOOK_SECONDS)
            duration = IDLE_LOOK_SECONDS
        elif behaviour == "body_tilt":
            ok = self._session.set_idle(PilotInput(gesture="body_tilt"), IDLE_TILT_SECONDS)
            duration = IDLE_TILT_SECONDS
        elif behaviour == "stretch":
            duration = GESTURE_PLAY_SECONDS["stretch"]
            ok = self._session.set_idle(PilotInput(gesture="stretch"), duration)
        else:
            ok = self._session.set_idle_face(self._rng.choice(IDLE_FACES), IDLE_FACE_SECONDS)
            duration = IDLE_FACE_SECONDS
        if ok:
            _LOG.info("AI idle: %s", behaviour)
            if self._telemetry is not None:
                self._telemetry.event("idle", behaviour=behaviour)
        return duration


class Idle:
    """Drives an `IdleJudge` until cancelled.

    Args:
        session: The pilot session idle writes to.
        rng: Random source for behaviour picks and pauses.
        telemetry: Receives an `idle` event per behaviour.
        period: Seconds between judge updates.
        now: Monotonic clock.
    """

    def __init__(
        self,
        session: PilotSession,
        *,
        rng: random.Random | None = None,
        telemetry: Telemetry | None = None,
        period: float = IDLE_PERIOD_S,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._judge = IdleJudge(session, rng=rng, telemetry=telemetry)
        self._period = period
        self._now = now

    async def run(self) -> None:
        """Run until cancelled. Never raises anything but cancellation: idle must not end an AI session."""
        logged = False
        try:
            while True:
                try:
                    self._judge.update(self._now())
                except Exception:
                    if not logged:
                        _LOG.warning("idle behaviour failed; skipping", exc_info=True)
                        logged = True
                await asyncio.sleep(self._period)
        finally:
            self._judge.stop()


__all__ = ["Idle", "IdleJudge"]
