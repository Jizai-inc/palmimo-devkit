"""ReflexEngine -- immediate, non-LLM reactions to what the robot notices.

Arbitrating reflexes against an in-flight LLM tool call would need a pending
queue with priority/preemption semantics; this example instead takes the
simplest rule that still avoids stepping on the conductor's idle/respond
turns: **if the robot is busy, skip the reflex entirely** -- no queueing, no
preemption, no priority. A missed wave-back or a skipped glance is an
acceptable trade for not reaching into
:class:`~palmimo_companion_agent.pipeline.conductor.Conductor`'s cancellation
machinery from a second, independent caller.

A reflex must also be skipped while the robot is asleep (a limp, reduced-gain
stance -- see ``sleep``/``wake_up`` in :mod:`.tools`): waving back or
tracking a face would stretch or turn a robot in that state, the same hazard
:class:`~palmimo_companion_agent.pipeline.sleeping.Sleeping` gates the idle
loop against. Rather than importing that pipeline-only class (core/ must not
depend on pipeline/ -- see below), the caller injects a plain
``inhibited`` predicate.

Three reflexes exist: a detected wave gets waved back at, a detected face
gets a glance, and hearing the robot's own name gets an acknowledgement. Both dispatch through the same
:class:`~palmimo_sdk.agent.toolset.AgentToolSet` the conductor's own turns
dispatch tool calls through (never a direct facade call, and never through
the LLM), and each reports what it did through a ``notify`` callback rather
than a direct :class:`~palmimo_companion_agent.pipeline.history.History`
dependency -- core/ (this module included) must not import from pipeline/,
since a future realtime/ runtime reuses this engine over its own event sink.
The caller (:mod:`~palmimo_companion_agent.pipeline.wiring`) wires ``notify``
to append a :class:`~palmimo_companion_agent.pipeline.history.SystemNoteEvent`
so the next idle or respond turn has the context ("you just waved back")
without having chosen to act itself.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING

from .perception import Detection, DetectionKind
from .tools import LOOK_AT_FACE_SEEN_MARK


if TYPE_CHECKING:
    from palmimo_sdk.agent.toolset import AgentToolSet


_log = logging.getLogger(__name__)

#: Face expression shown while waving back (the SDK's own uppercase vocabulary).
_WAVE_FACE = "HAPPY"
#: Face shown the instant the robot hears its name. Deliberately the FIRST
#: thing the name reflex does: it needs no camera and no face in frame, so it
#: is the only part guaranteed to happen. Turning to look is the bonus on top.
_NAME_CALL_FACE = "HAPPY"
#: Seconds the name reflex tracks the caller's face for, once it has answered.
_NAME_CALL_LOOK_SECONDS = 3.0
#: Seconds look_at_face tracks the face for on a "face" detection.
_FACE_LOOK_SECONDS = 3.0
#: How long look_at_face tolerates losing the face before giving up early.
_FACE_LOST_GRACE_SECONDS = 1.0

#: Per-kind cooldown (seconds): a reflex that just fired won't fire again for
#: this long, even once the robot is free -- a continuously-waving hand (or a
#: face that never leaves frame) must not retrigger every detection.
_COOLDOWNS: dict[DetectionKind, float] = {
    DetectionKind.WAVE: 8.0,
    DetectionKind.FACE: 15.0,
    # Shorter than the others on purpose: the other two fire off a state that
    # persists (a hand stays raised, a face stays in frame), so they need a long
    # cooldown to avoid retriggering on the same event. A name call is a
    # discrete act, and someone who calls twice meant it twice.
    DetectionKind.NAME_CALL: 3.0,
}


class ReflexEngine:
    """Drives reflexes off a Detection stream through the shared :class:`~palmimo_sdk.agent.toolset.AgentToolSet`.

    Args:
        toolset: The same toolset the conductor's idle/respond turns dispatch
            through -- a reflex only fires when ``toolset.is_busy()`` is
            False, so it never fights an in-flight tool call for the hardware.
        notify: Called with a human-readable line each time a reflex fires
            successfully. The caller decides where that goes (history, a log,
            ...) -- this class has no opinion beyond "something happened".
        inhibited: Checked alongside ``toolset.is_busy()`` -- a reflex is
            skipped whenever this returns True. Defaults to never inhibited.
            The intended use is a sleep-state predicate (e.g.
            ``lambda: sleeping.asleep``, wired from
            :mod:`~palmimo_companion_agent.pipeline.wiring`): a reflex must
            not wave or track a limp, reduced-gain sleeping robot.
    """

    def __init__(
        self,
        toolset: AgentToolSet,
        notify: Callable[[str], None],
        *,
        inhibited: Callable[[], bool] = lambda: False,
    ) -> None:
        self._toolset = toolset
        self._notify = notify
        self._inhibited = inhibited
        self._last_fired: dict[DetectionKind, float] = {}

    async def run(self, detections: AsyncIterator[Detection]) -> None:
        """Consume *detections* until the iterator ends (e.g. the source's ``aclose()``)."""
        async for detection in detections:
            await self._handle(detection)

    async def _handle(self, detection: Detection) -> None:
        cooldown = _COOLDOWNS.get(detection.kind)
        if cooldown is None:
            return  # No reflex configured for this kind.
        now = time.monotonic()
        last = self._last_fired.get(detection.kind)
        if last is not None and now - last < cooldown:
            return
        if self._toolset.is_busy() or self._inhibited():
            # Skip outright rather than queue: see the module docstring for why.
            return
        self._last_fired[detection.kind] = now
        try:
            if detection.kind is DetectionKind.WAVE:
                await self._on_wave(detection)
            elif detection.kind is DetectionKind.FACE:
                await self._on_face(detection)
            elif detection.kind is DetectionKind.NAME_CALL:
                await self._on_name_call(detection)
        except Exception:
            _log.warning("reflex for kind=%s failed", detection.kind, exc_info=True)

    async def _on_wave(self, detection: Detection) -> None:
        await self._toolset.call("wave_both", {"face": _WAVE_FACE, "reason": "reflex: waving back"})
        self._notify(f"[reflex] waved back at {detection.summary}.")

    async def _on_name_call(self, detection: Detection) -> None:
        """Answer a call: show a face at once, then look for whoever called.

        The expression goes first and is reported on its own, because it is what
        happens on every robot. ``look_at_face`` needs a camera and a face in
        frame, and someone calling from across the room or from behind has
        neither -- treating the glance as the acknowledgement would leave those
        calls with no visible answer at all.
        """
        await self._toolset.call("show_emoji", {"emoji": _NAME_CALL_FACE, "reason": "reflex: heard my name"})
        self._notify(f"[reflex] answered a call ({detection.summary}).")
        result = await self._toolset.call(
            "look_at_face",
            {
                "seconds": _NAME_CALL_LOOK_SECONDS,
                "lost_grace": _FACE_LOST_GRACE_SECONDS,
                "reason": "reflex: looking for whoever called",
            },
        )
        if LOOK_AT_FACE_SEEN_MARK in result.text:
            self._notify("[reflex] found the caller and turned to them.")

    async def _on_face(self, detection: Detection) -> None:
        result = await self._toolset.call(
            "look_at_face",
            {
                "seconds": _FACE_LOOK_SECONDS,
                "lost_grace": _FACE_LOST_GRACE_SECONDS,
                "reason": "reflex: noticed a face",
            },
        )
        if LOOK_AT_FACE_SEEN_MARK in result.text:
            self._notify(f"[reflex] noticed and looked at a face ({detection.summary}).")


__all__ = ["ReflexEngine"]
