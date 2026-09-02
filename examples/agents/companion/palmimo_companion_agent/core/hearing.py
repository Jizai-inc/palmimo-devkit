"""NameCallWatch — turning "someone said the robot's name" into a Detection.

The counterpart to :mod:`.vision` for the ear. It owns no microphone and no
model: the speech pipeline already transcribes every utterance, so this listens
to that text and decides, with :class:`palmimo_sdk.NameMatcher`, whether the
robot was called.

Matching is on sounds rather than spelling because the name is not a word of
the transcription language, so a transcriber writes it differently nearly every
time -- see :mod:`palmimo_sdk.name_match`.

Why a reflex at all, when the transcript is on its way to the LLM anyway: being
called deserves an answer *now*. The conversation turn behind it takes seconds
(transcribe, decide, speak), and a robot that stares blankly through those
seconds reads as not having heard. The reflex is the "yes?" that buys them.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from palmimo_sdk import NameMatcher

from .perception import Detection, DetectionKind


_log = logging.getLogger(__name__)

#: Dropped rather than queued when nobody is consuming detections fast enough.
#: A backlog of "you were called" is worse than useless -- answering a call from
#: ten seconds ago is a bug, not a courtesy.
_QUEUE_LIMIT = 4


class NameCallWatch:
    """Yields a :data:`~.perception.DetectionKind.NAME_CALL` when a transcript calls the robot.

    What the robot answers to is fixed by the SDK
    (:data:`palmimo_sdk.PALMIMO_NAMES`), not configurable here -- see
    :class:`palmimo_sdk.NameMatcher` for why.

    The producer side is :meth:`heard`, which the speech pipeline calls from
    whatever thread or task transcribed the utterance; the consumer side is
    :meth:`watch`, an async iterator for
    :class:`~palmimo_companion_agent.core.reflexes.ReflexEngine`.
    """

    def __init__(self) -> None:
        self._matcher = NameMatcher()
        self._queue: asyncio.Queue[Detection] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self._loop: asyncio.AbstractEventLoop | None = None

    def heard(self, transcript: str) -> None:
        """Offer one transcript. Fires a detection if it called the robot.

        Safe to call from another thread once :meth:`watch` is running; before
        that the transcript is dropped, since there is nothing to react with
        yet. Never raises into the caller -- the speech pipeline's job is to
        transcribe, and a reflex that cannot be queued must not cost it an
        utterance.
        """
        found = self._matcher.match(transcript)
        if found is None:
            return
        loop = self._loop
        if loop is None:
            _log.debug("name call heard before the reflex loop started; dropping")
            return
        summary = "someone called the robot by name"
        if found.command:
            summary = f"someone called the robot by name and said {found.command!r}"
        detection = Detection(summary=summary, kind=DetectionKind.NAME_CALL)
        try:
            loop.call_soon_threadsafe(self._offer, detection)
        except RuntimeError:  # loop already closed -- shutting down
            _log.debug("name call heard after the reflex loop closed; dropping")

    def _offer(self, detection: Detection) -> None:
        try:
            self._queue.put_nowait(detection)
        except asyncio.QueueFull:
            # See _QUEUE_LIMIT: a stale call is not worth answering.
            _log.debug("reflex queue full; dropping a name call")

    async def watch(self) -> AsyncIterator[Detection]:
        """Yield detections as calls arrive. Runs until cancelled."""
        self._loop = asyncio.get_running_loop()
        while True:
            yield await self._queue.get()


__all__ = ["NameCallWatch"]
