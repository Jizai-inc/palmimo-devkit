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
from typing import Any

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
        #: The behaviour started last and when it was planned to end, to tell
        #: "ran its course" from "cut short" (and by what) in the logs.
        self._running: tuple[str, float, float] | None = None

    def update(self, now: float) -> None:
        """Act on the session's state at time *now*: wait, start a behaviour, or let one run."""
        session = self._session
        self._check_running(now)
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
        duration = self._play(behaviour, now)
        pause = self._rng.uniform(*IDLE_PAUSE_S)
        self._next_at = now + duration + pause
        _LOG.debug("AI idle: next behaviour in %.1f s", duration + pause)

    def stop(self) -> None:
        """End any idle behaviour, recentering the neck."""
        if self._running is not None:
            self._end_event(self._running[0], "stopped", None)
            self._running = None
        self._session.clear_idle()

    def _check_running(self, now: float) -> None:
        if self._running is None:
            return
        behaviour, started, planned_end = self._running
        if now >= planned_end:
            self._running = None
            return
        session = self._session
        still_on = session.idle_face_active if behaviour == "face" else session.idle_active
        if still_on:
            return
        self._running = None
        if session.mode != MODE_AI:
            reason = "session_end"
        elif session.ai_busy:
            reason = "ai_intent"
        elif session.gazing:
            reason = "gaze"
        elif session.face_active:
            reason = "ai_face"
        else:
            reason = "other"
        self._end_event(behaviour, reason, round(now - started, 2))

    def _end_event(self, behaviour: str, reason: str, ran_s: float | None) -> None:
        _LOG.info("AI idle: %s cut short (%s)", behaviour, reason)
        if self._telemetry is not None:
            self._telemetry.event("idle_end", behaviour=behaviour, reason=reason, ran_s=ran_s)

    def _play(self, behaviour: str, now: float) -> float:
        """Start *behaviour* and return how long it runs. A refused write just skips this turn."""
        params: dict[str, Any] = {}
        if behaviour == "look_around":
            pitch = self._rng.uniform(-IDLE_NECK_MAX, IDLE_NECK_MAX)
            yaw = self._rng.uniform(-IDLE_NECK_MAX, IDLE_NECK_MAX)
            params = {"pitch": round(pitch, 3), "yaw": round(yaw, 3)}
            ok = self._session.set_idle(PilotInput(neck_pitch=pitch, neck_yaw=yaw), IDLE_LOOK_SECONDS)
            duration = IDLE_LOOK_SECONDS
        elif behaviour == "body_tilt":
            ok = self._session.set_idle(PilotInput(gesture="body_tilt"), IDLE_TILT_SECONDS)
            duration = IDLE_TILT_SECONDS
        elif behaviour == "stretch":
            duration = GESTURE_PLAY_SECONDS["stretch"]
            ok = self._session.set_idle(PilotInput(gesture="stretch"), duration)
        else:
            expression = self._rng.choice(IDLE_FACES)
            params = {"expression": expression}
            ok = self._session.set_idle_face(expression, IDLE_FACE_SECONDS)
            duration = IDLE_FACE_SECONDS
        if not ok:
            _LOG.debug("AI idle: %s refused by the session (%s)", behaviour, self._refusal())
            return duration
        self._running = (behaviour, now, now + duration)
        _LOG.info("AI idle: %s %s for %.1f s", behaviour, params or "", duration)
        if self._telemetry is not None:
            self._telemetry.event("idle", behaviour=behaviour, duration_s=duration, **params)
        return duration

    def _refusal(self) -> str:
        session = self._session
        if session.ai_busy:
            return "ai_busy"
        if session.gazing:
            return "gaze"
        if session.face_active:
            return "face showing"
        return "not in AI mode" if session.mode != MODE_AI else "unknown"


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
