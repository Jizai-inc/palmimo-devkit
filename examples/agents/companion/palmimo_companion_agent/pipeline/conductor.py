"""Conductor -- owns the bus + history and alternates between two turn types.

1. **Idle turn** (:class:`~.idle.IdleTurn`, autonomous): ReAct-style, one tool
   per tick, a restricted speech-free :class:`~.toolview.ToolView`, relaxed
   pacing (a random pause between ticks -- see :meth:`Conductor._idle_pause_seconds`).
2. **Respond turn** (:class:`~.respond.RespondTurn`, event-driven): a SINGLE
   LLM call with the FULL toolset, up to 4 tool calls executed sequentially
   -- see that class's own module docstring for the single-shot design.

Reflexes (:mod:`palmimo_companion_agent.core.reflexes`) dispatch through the same
shared :class:`~palmimo_sdk.agent.toolset.AgentToolSet`, independently of
both turn types -- the conductor never arbitrates between them and a reflex.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Any, TextIO

from ..core.toolview import IDLE_TOOL_NAMES, ToolView
from .bus import Bus, Interrupt
from .event_log import emit_event
from .history import Event, History, KeyboardEvent, MicInputEvent, SystemNoteEvent
from .idle import IdleTurn
from .llm import SpeechVerdict
from .respond import RespondTurn
from .sleeping import Sleeping


if TYPE_CHECKING:
    from palmimo_sdk.agent.toolset import AgentToolSet

    from .llm import LlmProvider


_log = logging.getLogger(__name__)

#: Bounds of the random pause between idle ticks (see :meth:`Conductor._idle_pause_seconds`).
_IDLE_PAUSE_MIN_SECONDS = 2.0
_IDLE_PAUSE_MAX_SECONDS = 6.0

_STARTUP_NOTE = "[startup] Take a moment to notice your surroundings, then start living on your own."


def _event_for_interrupt(interrupt: Interrupt) -> Event:
    """Translate one drained :class:`~.bus.Interrupt` into the History event it becomes.

    ``"speech"`` carries its ``directed`` flag through to
    :class:`~.history.MicInputEvent` -- see that class's docstring.
    ``"keyboard"`` has no such distinction: always addressed to the robot.
    """
    if interrupt.kind == "speech":
        return MicInputEvent(interrupt.text, directed=interrupt.directed)
    return KeyboardEvent(interrupt.text)


class Conductor:
    """Drains the input bus and alternates between a respond turn and idle ticks.

    Args:
        toolset: Wrapped in two :class:`~.toolview.ToolView`\\ s built
            internally -- a restricted, speech-squashed one for idle, and an
            unrestricted one for respond.
        event_log: An already-open stream to mirror every history event to as
            JSONL. None (default) disables logging.
    """

    def __init__(
        self,
        history: History,
        toolset: AgentToolSet,
        llm: LlmProvider,
        bus: Bus,
        *,
        event_log: TextIO | None = None,
    ) -> None:
        self.history = history
        self.toolset = toolset
        self.llm = llm
        self.bus = bus
        if event_log is not None:
            self.history.subscribe(lambda event: emit_event(event, out=event_log))
        idle_view = ToolView(toolset, allow=IDLE_TOOL_NAMES, squash_say=True)
        respond_view = ToolView(toolset, allow=None, squash_say=False)
        self._idle = IdleTurn(history, idle_view, llm, bus)
        self._respond = RespondTurn(history, respond_view, llm, bus)
        # Gates idle ticks while asleep (see run()) -- a History subscriber
        # rather than a hook inside execute_and_record, so dispatch.py stays
        # ignorant of this turn-scheduling concern. Public (not _sleeping) so
        # wiring.py can share this one instance with ReflexEngine's own
        # inhibited predicate -- a reflex must not wave/track a sleeping robot
        # either (see core/reflexes.py).
        self.sleeping = Sleeping()
        self.history.subscribe(self.sleeping.observe)
        # Keeps a strong reference to fire-and-forget barge-in tasks (see
        # _barge_in) until they finish -- asyncio only holds a weak
        # reference to a task once nothing else does, so an unreferenced
        # task can be garbage-collected before it runs.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------
    # Input -- called from the UI / speech pipeline
    # ------------------------------------------------------------------
    def submit_user_text(self, text: str, *, cancel_running: bool = True) -> None:
        """Post a typed instruction as an interrupt (history: :class:`~.history.KeyboardEvent`)."""
        self.bus.post(Interrupt(kind="keyboard", text=text, cancel_running=cancel_running))

    def submit_speech(self, verdict: SpeechVerdict, text: str) -> None:
        """Post guard-classified speech as an interrupt (history: :class:`~.history.MicInputEvent`).

        Args:
            verdict: The guard's classification. ``noise`` is dropped
                outright -- it never reaches the bus.
            text: Raw transcription, used as a fallback when the guard
                didn't return its own cleaned ``verdict.text``.

        A command or question cancels whatever the robot is doing and
        demands a spoken reply; ambient chatter does neither. ``cancel_running``
        and ``directed`` happen to coincide today (both are "command or
        question"), but stay separate :class:`~.bus.Interrupt` fields --
        they answer different questions and could diverge later.
        """
        if verdict.kind == "noise":
            _log.info("dropping noise verdict: %r", text)
            return
        spoken = verdict.text or text
        directed = verdict.kind in ("command", "question")
        self.bus.post(Interrupt(kind="speech", text=spoken, cancel_running=directed, directed=directed))
        if directed:
            self._barge_in()

    async def hear(self, text: str) -> None:
        """Guard-classify *text* as if it were a fresh STT result, then :meth:`submit_speech` it.

        A headless-testing entry point (see ``ui.cli``'s ``/hear`` command):
        drives the same guard -> submit_speech path real mic input goes
        through, without a microphone. On a classify_speech failure, falls
        back to treating *text* as a command -- same fallback
        :class:`~palmimo_companion_agent.pipeline.speech.SpeechPipeline` uses.
        """
        try:
            verdict = await self.llm.classify_speech(text)
        except Exception as exc:
            _log.warning("classify_speech failed, falling back to command: %s", exc)
            verdict = SpeechVerdict(kind="command", text=text)
        self.submit_speech(verdict, text)

    def _barge_in(self) -> None:
        """Interrupt in-flight motion and speech immediately, instead of waiting
        for :meth:`run`'s bus drain. See
        :meth:`~palmimo_sdk.agent.toolset.AgentToolSet.cancel_running` for why
        this must fire even when no tool call is in flight. Best-effort when no
        event loop is running yet.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.toolset.cancel_running())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(self._log_barge_in_failure)

    @staticmethod
    def _log_barge_in_failure(task: asyncio.Task[Any]) -> None:
        """Surface a failure from the fire-and-forget :meth:`_barge_in` task --
        nothing else awaits it, so an exception would otherwise vanish silently."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.warning("barge-in cancel_running() failed: %s", exc, exc_info=exc)

    # ------------------------------------------------------------------
    # The main loop
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Run until the enclosing task is cancelled.

        Each iteration drains queued interrupts into history. Any queued
        input (speech or keyboard -- :class:`~.bus.Interrupt` has no other
        ``kind``) triggers a single :class:`~.respond.RespondTurn`, told
        whether the batch included directed speech (see
        :meth:`~.respond.RespondTurn.run`), then immediately re-checks the
        bus so a second instruction arriving while responding is picked up
        right away. Otherwise, one :class:`~.idle.IdleTurn` tick runs UNLESS
        :attr:`sleeping` reports the robot asleep -- a successful ``sleep``
        tool call must not be immediately followed by an idle tick
        stretching or turning a limp robot at reduced gain -- and the loop
        then waits up to :meth:`_idle_pause_seconds` for the next interrupt
        -- one arriving during that wait is returned by :meth:`~.bus.Bus.wait`
        and carried into the next iteration rather than lost. The bus is
        still drained and a queued respond turn still runs while asleep --
        that is how ``wake_up`` (a respond-only tool) gets called at all.
        """
        self.history.add(SystemNoteEvent(_STARTUP_NOTE))
        pending: list[Interrupt] = []

        while True:
            interrupts = [*pending, *self.bus.drain()]
            pending = []
            for interrupt in interrupts:
                self.history.add(_event_for_interrupt(interrupt))

            if interrupts:
                directed_speech = any(it.kind == "speech" and it.directed for it in interrupts)
                await self._respond.run(directed_speech=directed_speech)
                continue

            if not self.sleeping.asleep:
                await self._idle.tick()
            pending = await self.bus.wait(self._idle_pause_seconds())

    @staticmethod
    def _idle_pause_seconds() -> float:
        """A random pause between idle ticks, giving the loop a relaxed, non-metronomic beat."""
        return random.uniform(_IDLE_PAUSE_MIN_SECONDS, _IDLE_PAUSE_MAX_SECONDS)


__all__ = ["Conductor"]
