"""Interrupt queue feeding the conductor's respond/idle turns.

:class:`Interrupt` bundles an observation's text with whether posting it
should cancel an in-flight long-running tool. :class:`Bus` is the queue plus
cancel flag :class:`~palmimo_companion_agent.pipeline.conductor.Conductor` drains
each iteration and :func:`~palmimo_companion_agent.pipeline.dispatch.run_tool`
races long-running tool calls against.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Interrupt:
    """A piece of input for the conductor to react to.

    Args:
        kind: ``"speech"`` (guarded microphone input) or ``"keyboard"``.
        cancel_running: Whether posting this should cancel an in-flight
            long-running tool so the new input takes over immediately.
        directed: Whether this input was addressed to the robot. False marks
            overheard ambient speech (guard verdict ``"ambient"``) -- it
            neither cancels a running tool nor demands a spoken reply.
            Always True for keyboard input.
    """

    kind: Literal["speech", "keyboard"]
    text: str
    cancel_running: bool = True
    directed: bool = True


class Bus:
    """Interrupt queue + cancel flag shared between input sources and the loop."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Interrupt] = asyncio.Queue()
        #: Set whenever a queued interrupt requested cancel_running=True.
        #: The loop races a long-running tool call against this event and
        #: clears it once consumed -- see dispatch._run_cancellable.
        self.cancel = asyncio.Event()

    def post(self, interrupt: Interrupt) -> None:
        """Queue *interrupt*; set :attr:`cancel` if it should stop an in-flight tool."""
        self._queue.put_nowait(interrupt)
        if interrupt.cancel_running:
            self.cancel.set()

    def drain(self) -> list[Interrupt]:
        """Return every interrupt queued so far, without blocking.

        Handing out interrupts also consumes their cancel signal, in the same
        synchronous block (no await point in between) -- so a post() can never
        slip between the drain and the clear, whatever order the caller does
        things in. A post() after this returns simply sets :attr:`cancel`
        again for the next consumer.
        """
        drained: list[Interrupt] = []
        while not self._queue.empty():
            drained.append(self._queue.get_nowait())
        if drained:
            self.cancel.clear()
        return drained

    async def wait(self, timeout: float) -> list[Interrupt]:
        """Wait up to *timeout* seconds for the first interrupt, then drain the rest.

        Returns an empty list on timeout. Used by the idle wait so the loop
        wakes promptly on new input while still heartbeating when none
        arrives. Like :meth:`drain`, consumes the cancel signal.
        """
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return []
        rest = self.drain()
        self.cancel.clear()
        return [first, *rest]
