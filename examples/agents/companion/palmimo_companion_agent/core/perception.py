"""What the reflex layer reacts to, independent of which sense noticed it.

:class:`Detection` lives here rather than in :mod:`.vision` because both
sight and hearing are wired to a reflex (see :mod:`.hearing`), and a module
named ``vision`` is the wrong home for a type an ear produces -- so the
vocabulary lives here and both senses import it.

:func:`merge` is what lets several senses share one
:class:`~palmimo_companion_agent.core.reflexes.ReflexEngine`: the engine
consumes a single stream, and arbitrating "eyes vs ears" is not its job.
"""

from __future__ import annotations

import asyncio
import enum
from collections.abc import AsyncIterator
from dataclasses import dataclass


class DetectionKind(enum.StrEnum):
    """The fixed vocabulary of things a reflex can fire on.

    A typo here is a mypy error, not a silent no-op.
    """

    WAVE = "wave"
    FACE = "face"
    NAME_CALL = "name_call"


@dataclass(frozen=True)
class Detection:
    """One observation the reflex layer can react to.

    Attributes:
        summary: Human-readable observation text (for
            :class:`~palmimo_companion_agent.pipeline.history.SystemNoteEvent`
            / logging).
        kind: The detector's kind key. Cooldowns and
            :class:`~palmimo_companion_agent.core.reflexes.ReflexEngine`'s
            dispatch are both keyed on this. A plain ``str`` subclass
            (:class:`DetectionKind` is a ``StrEnum``), so it still
            serializes/compares like a string (e.g. through
            :mod:`~palmimo_companion_agent.pipeline.event_log`'s JSONL).
    """

    summary: str
    kind: DetectionKind


async def merge(*sources: AsyncIterator[Detection]) -> AsyncIterator[Detection]:
    """Interleave several detection streams into one, in arrival order.

    Ends when every source has ended. A source that raises takes the merged
    stream down with it, deliberately: a sense that has failed should surface
    rather than leave the robot half-reflexive with no indication of which half.

    With a single source this is a pass-through, so the caller does not need to
    branch on how many senses happen to be wired.
    """
    if len(sources) == 1:
        async for detection in sources[0]:
            yield detection
        return

    queue: asyncio.Queue[tuple[Detection | None, BaseException | None]] = asyncio.Queue()

    async def pump(source: AsyncIterator[Detection]) -> None:
        try:
            async for detection in source:
                await queue.put((detection, None))
        except BaseException as exc:  # re-raised on the consumer side below
            await queue.put((None, exc))
        else:
            await queue.put((None, None))

    tasks = [asyncio.ensure_future(pump(source)) for source in sources]
    try:
        finished = 0
        while finished < len(tasks):
            item, error = await queue.get()
            if error is not None:
                raise error
            if item is None:
                finished += 1
                continue
            yield item
    finally:
        for task in tasks:
            task.cancel()


__all__ = ["Detection", "DetectionKind", "merge"]
