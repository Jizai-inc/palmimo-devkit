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

from palmimo_sdk.engine import MotionEngine

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
#: Offset from the frame center below which the neck does not move.
GLANCE_DEADBAND: float = 0.04
#: Least time between two neck corrections. A frame is several frames old by
#: the time its face is located, so correcting on every frame stacks the same
#: correction again and again and runs the neck into its stop; waiting lets
#: the neck arrive and the camera catch up before the next look.
GLANCE_UPDATE_S: float = 0.3
#: The head camera's approximate field of view, used to turn a face's offset
#: in the frame into neck travel. Not measured; `GLANCE_DAMPING` keeps an
#: estimate that is off by up to 2x from overshooting.
CAMERA_HFOV_DEG: float = 60.0
CAMERA_VFOV_DEG: float = 45.0
#: Fraction of the measured offset each correction removes.
GLANCE_DAMPING: float = 0.5
#: Furthest a glance turns the neck on either axis, as a fraction of its
#: travel: clear of the mechanical stops, and easier on the neck's heat.
GLANCE_MAX: float = 0.6
#: Which way the neck turns for a face right of / below the frame center.
#: `Palmimo.look` leaves the yaw sign-to-direction mapping unspecified, so
#: flip a sign here if a glance turns away from the face.
GLANCE_YAW_SIGN: int = 1
GLANCE_PITCH_SIGN: int = 1
#: Largest face's width, as a fraction of the frame width, at which a visitor
#: counts as "close": roughly a face leaning in over the robot.
BOW_NEAR_FACE_FRACTION: float = 0.3
#: Consecutive "close" frames before a bow; filters a face passing by the lens.
BOW_NEAR_HITS: int = 5
#: Seconds after a bow before another can start, so a crowd does not keep it bowing.
BOW_COOLDOWN_S: float = 30.0
#: How often `Reflexes` logs what the camera saw (`reflex_stats`), for tuning thresholds.
REFLEX_STATS_INTERVAL_S: float = 30.0
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


def _clamp(value: float, limit: float = 1.0) -> float:
    return max(-limit, min(limit, value))


class ReflexJudge:
    """Turns per-frame sightings into session writes. Pure: no I/O, clock passed in."""

    def __init__(
        self,
        session: PilotSession,
        *,
        telemetry: Telemetry | None = None,
        on_new_face: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        self._telemetry = telemetry
        #: Called when a glance starts at a face, so the model can be shown who arrived.
        self._on_new_face = on_new_face
        self._last_wave: float | None = None
        self._last_glance: float | None = None
        self._face_hits = 0
        self._glance_started: float | None = None
        self._glance_face_size: float | None = None
        self._face_seen_at = 0.0
        #: When the glance last corrected the neck, `None` before its first correction.
        self._last_correction: float | None = None
        self._pitch = 0.0
        self._yaw = 0.0
        self._last_bow: float | None = None
        self._near_hits = 0
        #: Whether the next close approach may bow. Cleared by a bow and set
        #: again only once the face moves back or leaves, so a visitor who stays
        #: close is bowed to once, not every cooldown.
        self._bow_armed = True
        #: Whether this approach's skipped bow was already logged, so a
        #: skip is logged once per approach rather than on every frame.
        self._bow_skip_logged = False
        #: Waves seen but not waved back (busy or cooling down) since the last wave-back.
        self._waves_skipped = 0

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
            if not self._bow_armed:
                # Logged because a flickering detector re-arms here too, which is
                # what lets a visitor who never left be bowed to again.
                reason = "face_lost" if sighting.face_size is None else "face_far"
                _LOG.info("AI reflex: bow re-armed (%s, face_size=%s)", reason, _round(sighting.face_size))
                self._event("bow_rearm", reason=reason, face_size=_round(sighting.face_size))
            self._bow_armed = True
            self._bow_skip_logged = False
        if sighting.waved and self._wave_back(now):
            return
        if near and self._maybe_bow(sighting.face_size, now):
            return
        if self.glancing:
            self._continue_glance(sighting, now)
        else:
            self._maybe_start_glance(sighting, now)

    def stop(self) -> None:
        """End any glance, recentering the neck."""
        if self.glancing:
            self._end_glance("stopped", None)

    def _wave_back(self, now: float) -> bool:
        if self._session.ai_busy or (self._last_wave is not None and now - self._last_wave < WAVE_COOLDOWN_S):
            self._waves_skipped += 1
            _LOG.debug("AI reflex: wave seen but skipped (%s)", "ai_busy" if self._session.ai_busy else "cooldown")
            return False
        if not self._session.set_ai_intent(PilotInput(gesture="wave"), GESTURE_PLAY_SECONDS["wave"]):
            return False
        since = None if self._last_wave is None else round(now - self._last_wave, 1)
        self._last_wave = now
        if self.glancing:
            self._end_glance("wave", now)
        self._face_hits = 0
        _LOG.info("AI reflex: waved back")
        self._event("wave", waves_skipped=self._waves_skipped, since_last_s=since)
        self._waves_skipped = 0
        return True

    def _maybe_bow(self, face_size: float | None, now: float) -> bool:
        # A skipped bow stays armed: the visitor is still close once the AI's motion ends.
        if not self._bow_armed or self._near_hits < BOW_NEAR_HITS:
            return False
        if self._session.ai_busy:
            self._log_bow_skip("ai_busy")
            return False
        if self._last_bow is not None and now - self._last_bow < BOW_COOLDOWN_S:
            self._log_bow_skip(f"cooldown, {BOW_COOLDOWN_S - (now - self._last_bow):.1f} s left")
            return False
        if not self._session.set_ai_intent(PilotInput(gesture="bow"), GESTURE_PLAY_SECONDS["bow"]):
            return False
        since = None if self._last_bow is None else round(now - self._last_bow, 1)
        self._last_bow = now
        self._bow_armed = False
        if self.glancing:
            self._end_glance("bow", now)
        self._face_hits = 0
        _LOG.info("AI reflex: bowed to a close visitor (face_size=%s)", _round(face_size))
        self._event("bow", face_size=_round(face_size), near_frames=self._near_hits, since_last_s=since)
        return True

    def _log_bow_skip(self, reason: str) -> None:
        if not self._bow_skip_logged:
            _LOG.debug("AI reflex: close visitor, bow skipped (%s)", reason)
            self._bow_skip_logged = True

    def _maybe_start_glance(self, sighting: Sighting, now: float) -> None:
        face = sighting.face
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
        self._glance_face_size = sighting.face_size
        self._pitch, self._yaw = 0.0, 0.0
        self._last_correction = None
        self._track(face, now)
        if self._on_new_face is not None:
            self._on_new_face()
        _LOG.info("AI reflex: glancing at a face")
        self._event(
            "glance",
            face=[_round(face[0]), _round(face[1])],
            face_size=_round(sighting.face_size),
            target_pitch=_round(self._pitch),
            target_yaw=_round(self._yaw),
        )

    def _continue_glance(self, sighting: Sighting, now: float) -> None:
        started = self._glance_started
        assert started is not None
        # An AI intent wins: the model asked for something, so the glance gives way.
        if self._session.ai_busy:
            self._end_glance("ai_intent", now)
        elif now - started >= GLANCE_SECONDS:
            self._end_glance("timeout", now)
        elif sighting.face is not None:
            self._track(sighting.face, now)
        elif now - self._face_seen_at >= GLANCE_LOST_GRACE_S:
            self._end_glance("face_lost", now)

    def _track(self, face: tuple[float, float], now: float) -> None:
        """Move the neck part of the way toward *face*, at most once per `GLANCE_UPDATE_S`.

        The offset is measured from where the neck already points, so a
        correction is the face's remaining distance, converted from a
        fraction of the frame to a fraction of neck travel.
        """
        self._face_seen_at = now
        if self._last_correction is not None and now - self._last_correction < GLANCE_UPDATE_S:
            return
        self._last_correction = now
        ex, ey = face[0] - 0.5, face[1] - 0.5
        if abs(ex) >= GLANCE_DEADBAND:
            step = ex * CAMERA_HFOV_DEG / MotionEngine.NECK_YAW_TRAVEL_DEG * GLANCE_DAMPING
            self._yaw = _clamp(self._yaw + GLANCE_YAW_SIGN * step, GLANCE_MAX)
        if abs(ey) >= GLANCE_DEADBAND:
            step = ey * CAMERA_VFOV_DEG / MotionEngine.NECK_PITCH_TRAVEL_DEG * GLANCE_DAMPING
            self._pitch = _clamp(self._pitch + GLANCE_PITCH_SIGN * step, GLANCE_MAX)
        self._session.set_gaze(self._pitch, self._yaw)
        _LOG.debug(
            "AI reflex: glance face=(%.3f, %.3f) -> pitch=%.3f yaw=%.3f", face[0], face[1], self._pitch, self._yaw
        )

    def _end_glance(self, reason: str, now: float | None) -> None:
        started = self._glance_started
        self._glance_started = None
        self._session.clear_gaze()
        duration = None if started is None or now is None else round(now - started, 2)
        _LOG.debug("AI reflex: glance ended (%s)", reason)
        self._event(
            "glance_end",
            reason=reason,
            duration_s=duration,
            final_pitch=_round(self._pitch),
            final_yaw=_round(self._yaw),
        )

    def _event(self, kind: str, **fields: Any) -> None:
        if self._telemetry is not None:
            self._telemetry.event("reflex", reflex=kind, **fields)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


class ReflexStats:
    """Counts what the camera saw over one window, for the periodic `reflex_stats` event.

    The numbers are what tuning needs: face sizes against
    `BOW_NEAR_FACE_FRACTION`, how often faces are in view, and the real frame rate.
    """

    def __init__(self, started: float) -> None:
        self._started = started
        self.frames = 0
        self.frame_errors = 0
        self.waves = 0
        self.near_frames = 0
        self._face_sizes: list[float] = []

    def record(self, sighting: Sighting) -> None:
        self.frames += 1
        if sighting.waved:
            self.waves += 1
        if sighting.face_size is not None:
            self._face_sizes.append(sighting.face_size)
            if sighting.face_size >= BOW_NEAR_FACE_FRACTION:
                self.near_frames += 1

    def record_error(self) -> None:
        self.frame_errors += 1

    def due(self, now: float) -> bool:
        return now - self._started >= REFLEX_STATS_INTERVAL_S

    def summary(self, now: float) -> dict[str, Any]:
        """This window's numbers as event fields."""
        elapsed = max(now - self._started, 1e-9)
        sizes = sorted(self._face_sizes)
        return {
            "window_s": round(elapsed, 1),
            "frames": self.frames,
            "fps": round(self.frames / elapsed, 1),
            "frame_errors": self.frame_errors,
            "face_frames": len(sizes),
            "face_size_p50": _round(_percentile(sizes, 0.5)),
            "face_size_p90": _round(_percentile(sizes, 0.9)),
            "face_size_max": _round(sizes[-1] if sizes else None),
            "near_frames": self.near_frames,
            "waves": self.waves,
        }


def _percentile(sorted_values: list[float], fraction: float) -> float | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, int(fraction * len(sorted_values)))
    return sorted_values[index]


class Reflexes:
    """Reads head-camera frames and drives a `ReflexJudge` until cancelled.

    Args:
        session: The pilot session reflexes write intents and gaze to.
        frame_source: Returns the newest camera frame, or `None`. Blocking; runs on a worker thread.
        load_detectors: Builds (or returns cached) `Detectors`. Blocking; runs on a worker thread.
        on_new_face: Called when a glance starts at a face (see `frames.FramePusher.nudge`).
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
        on_new_face: Callable[[], None] | None = None,
    ) -> None:
        self._frame_source = frame_source
        self._load_detectors = load_detectors
        self._judge = ReflexJudge(session, telemetry=telemetry, on_new_face=on_new_face)
        self._telemetry = telemetry
        self._period = period
        self._now = now
        self._last_failure_log_at: float | None = None
        self._stats = ReflexStats(now())

    async def run(self) -> None:
        """Watch and react until cancelled. Never raises anything but cancellation."""
        load_started = self._now()
        try:
            detectors = await asyncio.to_thread(self._load_detectors)
        except Exception:
            _LOG.warning("wave/face detectors unavailable; AI mode runs without reflexes", exc_info=True)
            return
        # The first load in a process imports MediaPipe and reads its models,
        # which delays the first reflex; this shows by how much.
        load_s = round(self._now() - load_started, 2)
        _LOG.info("AI reflexes: detectors ready in %.2f s", load_s)
        if self._telemetry is not None:
            self._telemetry.event("reflex_ready", load_s=load_s)
        self._stats = ReflexStats(self._now())
        try:
            while True:
                started = self._now()
                await self._step(detectors)
                self._maybe_emit_stats()
                await asyncio.sleep(max(0.0, self._period - (self._now() - started)))
        finally:
            self._emit_stats()
            self._judge.stop()

    async def _step(self, detectors: Detectors) -> None:
        try:
            frame = await asyncio.to_thread(self._frame_source)
            if frame is None:
                return
            sighting = await asyncio.to_thread(_look, detectors, frame, self._now())
        except Exception:
            self._stats.record_error()
            now = self._now()
            if self._last_failure_log_at is None or now - self._last_failure_log_at >= FAILURE_LOG_INTERVAL_S:
                _LOG.warning("reflex detectors failed on a frame", exc_info=True)
                self._last_failure_log_at = now
            return
        self._stats.record(sighting)
        self._judge.update(sighting, self._now())

    def _maybe_emit_stats(self) -> None:
        if self._stats.due(self._now()):
            self._emit_stats()

    def _emit_stats(self) -> None:
        now = self._now()
        summary = self._stats.summary(now)
        self._stats = ReflexStats(now)
        if summary["frames"] == 0 and summary["frame_errors"] == 0:
            return
        _LOG.info("AI reflex stats: %s", summary)
        if self._telemetry is not None:
            self._telemetry.event("reflex_stats", **summary)


def _look(detectors: Detectors, frame: Any, timestamp: float) -> Sighting:
    face = detectors.face.locate_box(frame)
    return Sighting(
        waved=detectors.wave.process(frame, timestamp),
        face=None if face is None else face.center,
        face_size=None if face is None else face.size,
    )


__all__ = ["Detectors", "ReflexJudge", "ReflexStats", "Reflexes", "Sighting"]
