"""Reflexes for a live AI session: wave back at a waving visitor, bow to one who comes
close, glance at a new face.

Ported from the companion agent's reflex engine, with its rules: a reflex
is skipped outright (never queued) while an AI intent runs, each kind has a
cooldown, and a glance tracks the face for a few seconds, then recenters.
The model is not told about reflexes; they are the robot's own body language.

Runs as one more task in the AI session's task group, on the server's event
loop, so it writes `session.PilotSession` from the same single writer as
everything else. Frame reads and MediaPipe inference run on worker threads.
A failing detector is logged and skipped: reflexes must never end an AI
session.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..session import GESTURE_PLAY_SECONDS, PilotInput, PilotSession
from ..telemetry import Telemetry
from .vision import FaceBox


_LOG = logging.getLogger(__name__)

#: Target rate for reading a frame and running the detectors.
REFLEX_PERIOD_S: float = 1.0 / 15.0
#: Seconds after a wave-back before another wave can trigger one.
WAVE_COOLDOWN_S: float = 8.0
#: Seconds after a glance starts before another can start, so a visitor who
#: stays in view draws an occasional glance rather than a constant stare
#: (the neck servo also heats when held off-center).
GLANCE_COOLDOWN_S: float = 15.0
#: Consecutive frames with a face before a glance starts; filters single-frame false positives.
GLANCE_FACE_HITS: int = 3
#: How long one glance tracks the face.
GLANCE_SECONDS: float = 3.0
#: How long a glance tolerates losing the face before ending early.
GLANCE_LOST_GRACE_S: float = 1.0
#: Proportional gain from the face's offset (fraction of the frame) to the neck target.
GLANCE_GAIN: float = 0.6
#: Offset from the frame center below which the neck does not move.
GLANCE_DEADBAND: float = 0.04
#: Largest face's width, as a fraction of the frame width, at which a visitor
#: counts as "close": roughly a face leaning in over the robot.
BOW_NEAR_FACE_FRACTION: float = 0.3
#: Consecutive "close" frames before a bow; filters a face passing by the lens.
BOW_NEAR_HITS: int = 5
#: Seconds after a bow before another can start, so a crowd does not keep it bowing.
BOW_COOLDOWN_S: float = 30.0
#: Minimum gap between two "detector failed" log lines.
FAILURE_LOG_INTERVAL_S: float = 30.0


class WaveDetectorLike(Protocol):
    def process(self, frame: Any, timestamp: float) -> bool: ...


class FaceLocatorLike(Protocol):
    def locate_box(self, frame: Any) -> FaceBox | None: ...


@dataclass(frozen=True)
class Detectors:
    """The loaded detectors. Building them loads MediaPipe models, so it happens once per process."""

    wave: WaveDetectorLike
    face: FaceLocatorLike


@dataclass(frozen=True)
class Sighting:
    """What one frame showed."""

    waved: bool = False
    face: tuple[float, float] | None = None
    #: The largest face's width as a fraction of the frame width, `None` with no face.
    face_size: float | None = None


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


class ReflexJudge:
    """Turns per-frame sightings into session writes. Pure: no I/O, clock passed in."""

    def __init__(self, session: PilotSession, *, telemetry: Telemetry | None = None) -> None:
        self._session = session
        self._telemetry = telemetry
        self._last_wave: float | None = None
        self._last_glance: float | None = None
        self._face_hits = 0
        self._glance_started: float | None = None
        self._face_seen_at = 0.0
        self._pitch = 0.0
        self._yaw = 0.0
        self._last_bow: float | None = None
        self._near_hits = 0
        #: Whether the next close approach may bow. Cleared by a bow and set
        #: again only once the face moves back or leaves, so a visitor who stays
        #: close is bowed to once, not every cooldown.
        self._bow_armed = True

    @property
    def glancing(self) -> bool:
        return self._glance_started is not None

    def update(self, sighting: Sighting, now: float) -> None:
        """Apply one frame's sighting at time *now*.

        Wave-back runs first, so a visitor who waves while leaning in gets a
        wave, not a bow, on that frame.
        """
        near = sighting.face_size is not None and sighting.face_size >= BOW_NEAR_FACE_FRACTION
        if near:
            self._near_hits += 1
        else:
            self._near_hits = 0
            self._bow_armed = True
        if sighting.waved and self._wave_back(now):
            return
        if near and self._maybe_bow(now):
            return
        if self.glancing:
            self._continue_glance(sighting.face, now)
        else:
            self._maybe_start_glance(sighting.face, now)

    def stop(self) -> None:
        """End any glance, recentering the neck."""
        if self.glancing:
            self._end_glance()

    def _wave_back(self, now: float) -> bool:
        if self._session.ai_busy:
            return False
        if self._last_wave is not None and now - self._last_wave < WAVE_COOLDOWN_S:
            return False
        if not self._session.set_ai_intent(PilotInput(gesture="wave"), GESTURE_PLAY_SECONDS["wave"]):
            return False
        self._last_wave = now
        if self.glancing:
            self._end_glance()
        self._face_hits = 0
        _LOG.info("AI reflex: waved back")
        self._event("wave")
        return True

    def _maybe_bow(self, now: float) -> bool:
        # A skipped bow stays armed: the visitor is still close once the AI's motion ends.
        if not self._bow_armed or self._near_hits < BOW_NEAR_HITS or self._session.ai_busy:
            return False
        if self._last_bow is not None and now - self._last_bow < BOW_COOLDOWN_S:
            return False
        if not self._session.set_ai_intent(PilotInput(gesture="bow"), GESTURE_PLAY_SECONDS["bow"]):
            return False
        self._last_bow = now
        self._bow_armed = False
        if self.glancing:
            self._end_glance()
        self._face_hits = 0
        _LOG.info("AI reflex: bowed to a close visitor")
        self._event("bow")
        return True

    def _maybe_start_glance(self, face: tuple[float, float] | None, now: float) -> None:
        if face is None:
            self._face_hits = 0
            return
        self._face_hits += 1
        if self._face_hits < GLANCE_FACE_HITS or self._session.ai_busy:
            return
        if self._last_glance is not None and now - self._last_glance < GLANCE_COOLDOWN_S:
            return
        self._face_hits = 0
        self._last_glance = now
        self._glance_started = now
        self._pitch, self._yaw = 0.0, 0.0
        _LOG.info("AI reflex: glancing at a face")
        self._event("glance")
        self._track(face, now)

    def _continue_glance(self, face: tuple[float, float] | None, now: float) -> None:
        started = self._glance_started
        assert started is not None
        # An AI intent wins: the model asked for something, so the glance gives way.
        if self._session.ai_busy or now - started >= GLANCE_SECONDS:
            self._end_glance()
        elif face is not None:
            self._track(face, now)
        elif now - self._face_seen_at >= GLANCE_LOST_GRACE_S:
            self._end_glance()

    def _track(self, face: tuple[float, float], now: float) -> None:
        self._face_seen_at = now
        ex, ey = face[0] - 0.5, face[1] - 0.5
        if abs(ex) >= GLANCE_DEADBAND:
            self._yaw = _clamp(self._yaw + ex * GLANCE_GAIN)
        if abs(ey) >= GLANCE_DEADBAND:
            self._pitch = _clamp(self._pitch + ey * GLANCE_GAIN)
        self._session.set_gaze(self._pitch, self._yaw)

    def _end_glance(self) -> None:
        self._glance_started = None
        self._session.clear_gaze()

    def _event(self, kind: str) -> None:
        if self._telemetry is not None:
            self._telemetry.event("reflex", reflex=kind)


class Reflexes:
    """Reads head-camera frames and drives a `ReflexJudge` until cancelled.

    Args:
        session: The pilot session reflexes write intents and gaze to.
        frame_source: Returns the newest camera frame, or `None`. Blocking; runs on a worker thread.
        load_detectors: Builds (or returns cached) `Detectors`. Blocking; runs on a worker thread.
    """

    def __init__(
        self,
        session: PilotSession,
        frame_source: Callable[[], Any | None],
        load_detectors: Callable[[], Detectors],
        *,
        telemetry: Telemetry | None = None,
        period: float = REFLEX_PERIOD_S,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._frame_source = frame_source
        self._load_detectors = load_detectors
        self._judge = ReflexJudge(session, telemetry=telemetry)
        self._period = period
        self._now = now
        self._last_failure_log_at: float | None = None

    async def run(self) -> None:
        """Watch and react until cancelled. Never raises anything but cancellation."""
        try:
            detectors = await asyncio.to_thread(self._load_detectors)
        except Exception:
            _LOG.warning("wave/face detectors unavailable; AI mode runs without reflexes", exc_info=True)
            return
        try:
            while True:
                started = self._now()
                await self._step(detectors)
                await asyncio.sleep(max(0.0, self._period - (self._now() - started)))
        finally:
            self._judge.stop()

    async def _step(self, detectors: Detectors) -> None:
        try:
            frame = await asyncio.to_thread(self._frame_source)
            if frame is None:
                return
            sighting = await asyncio.to_thread(_look, detectors, frame, self._now())
        except Exception:
            now = self._now()
            if self._last_failure_log_at is None or now - self._last_failure_log_at >= FAILURE_LOG_INTERVAL_S:
                _LOG.warning("reflex detectors failed on a frame", exc_info=True)
                self._last_failure_log_at = now
            return
        self._judge.update(sighting, self._now())


def _look(detectors: Detectors, frame: Any, timestamp: float) -> Sighting:
    face = detectors.face.locate_box(frame)
    return Sighting(
        waved=detectors.wave.process(frame, timestamp),
        face=None if face is None else face.center,
        face_size=None if face is None else face.size,
    )


__all__ = ["Detectors", "ReflexJudge", "Reflexes", "Sighting"]
