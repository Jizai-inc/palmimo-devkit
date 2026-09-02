"""ReflexRunner -- wave-back and glance reflexes, wired onto this runtime's own event sink.

Wraps :class:`~palmimo_companion_agent.core.reflexes.ReflexEngine` (unchanged
from ``core/``) over a :class:`~palmimo_companion_agent.core.vision.VisionWatch`.
:func:`build_reflex_runner` is the factory that wires the engine's ``notify``
callback to this runtime's own client instead of a
:class:`~palmimo_companion_agent.pipeline.history.History` -- no ``History``
exists over here (see ``state.py``'s module docstring for the same boundary).
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Callable

from palmimo_sdk.agent.toolset import AgentToolSet

from ...core.reflexes import ReflexEngine
from ...core.vision import VisionWatch
from ..client import RealtimeClientLike
from ..log import EventLog
from ..protocol import ItemCreate
from ..state import Sleeping


class ReflexRunner:
    """Drives :class:`ReflexEngine` off a :class:`VisionWatch`'s detection stream, until the session ends.

    Owns *notify_tasks*: the ``client.send(...)`` tasks the engine's own
    ``notify`` callback (see :func:`_build_notify`) detaches every time a
    reflex fires. Those sends are NOT children of :meth:`run`'s own task --
    they are scheduled from inside a synchronous callback, so a TaskGroup
    cancelling :meth:`run` never touches them -- which is exactly why
    :meth:`settle` exists: without it they would be left to finish or fail
    on their own after the session has already moved on to tearing down the
    socket, producing the "task exception was never retrieved" noise a
    cancelled/failed one leaves behind.
    """

    name = "reflexes"

    def __init__(self, watch: VisionWatch, engine: ReflexEngine, notify_tasks: set[asyncio.Task[None]]) -> None:
        self._watch = watch
        self._engine = engine
        self._notify_tasks = notify_tasks

    async def run(self) -> None:
        try:
            await self._engine.run(self._watch.watch())
        finally:
            await self._watch.aclose()

    async def settle(self, timeout: float) -> None:
        """Cancel every in-flight notify-send task and wait (bounded) for them to finish.

        Called from :class:`~..app.RealtimeSession._shutdown`, alongside
        :meth:`~..bridge.ToolBridge.settle` -- see this class's own docstring
        for why these tasks need their own settle instead of piggybacking on
        :meth:`run`'s cancellation.
        """
        tasks = list(self._notify_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout)


def _report_notify_failure(task: asyncio.Task[None]) -> None:
    """Surface a notify-send task that died, instead of letting it vanish (or print at GC time)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(f"!! reflex notify send failed: {exc!r}", file=sys.stderr, flush=True)


def _build_notify(client: RealtimeClientLike, log: EventLog, pending: set[asyncio.Task[None]]) -> Callable[[str], None]:
    """Build the ``notify`` callback: tells the model what a reflex just did.

    The reflex fires straight at the toolset (never through the model), so
    without this the robot waves back and then answers as though nothing had
    happened. Called from the reflex engine's own task, so there is a
    running event loop to schedule the send on -- the send itself is
    detached as a task rather than awaited, since ``notify`` is a plain
    synchronous callback. *pending* is shared with the :class:`ReflexRunner`
    this notify is built for (see :meth:`ReflexRunner.settle`) -- bounded via
    a done-callback discard either way, so it cannot grow across the
    session's lifetime even before shutdown.
    """

    def notify(text: str) -> None:
        print(f"reflex > {text}", flush=True)
        log.write("reflex", text=text)
        task = asyncio.ensure_future(client.send(ItemCreate.text(text)))
        pending.add(task)
        task.add_done_callback(pending.discard)
        task.add_done_callback(_report_notify_failure)

    return notify


def build_reflex_runner(
    toolset: AgentToolSet, watch: VisionWatch, client: RealtimeClientLike, sleeping: Sleeping, log: EventLog
) -> ReflexRunner:
    """Build a :class:`ReflexRunner` with its engine wired to *client* and inhibited while asleep."""
    pending: set[asyncio.Task[None]] = set()
    engine = ReflexEngine(toolset, notify=_build_notify(client, log, pending), inhibited=lambda: sleeping.asleep)
    return ReflexRunner(watch, engine, pending)


__all__ = ["ReflexRunner", "build_reflex_runner"]
