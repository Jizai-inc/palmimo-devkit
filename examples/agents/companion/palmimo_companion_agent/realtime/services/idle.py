"""IdlePacer + IdleTicker -- deciding when the robot should act unprompted.

Without this the robot is perfectly still between sentences, which idle.md
itself calls looking dead. :class:`IdlePacer` is the shared clock/flag both
:class:`~.router.EventRouter` (touches it on every speech/response boundary)
and :class:`IdleTicker` (reads it) use; :class:`IdleTicker` is the loop that
actually fires a tick when the pacer says it's due.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from ..client import RealtimeClientLike
from ..log import EventLog
from ..protocol import ResponseCreate
from ..state import Sleeping


#: Silence before the robot acts unprompted, drawn fresh each time. Matches
#: idle.md's pacing.
IDLE_MIN_SECONDS = 4.0
IDLE_MAX_SECONDS = 8.0

#: How often IdleTicker checks whether a tick is due.
_TICK_PERIOD_S = 0.5

#: Response metadata this runtime tags every idle tick with, so
#: :class:`~..bridge.ToolBridge` can tell an idle response from a respond one
#: without any claim/owns bookkeeping.
IDLE_TURN_METADATA: dict[str, str] = {"palmimo_turn": "idle"}


class IdlePacer:
    """Tracks whether a response is in flight and when the next idle tick is due."""

    def __init__(self) -> None:
        self.busy = False
        self._due = time.monotonic() + IDLE_MIN_SECONDS

    def touch(self) -> None:
        """Reset the due time to a fresh random point in the idle window."""
        self._due = time.monotonic() + random.uniform(IDLE_MIN_SECONDS, IDLE_MAX_SECONDS)

    def is_due(self) -> bool:
        """Whether an idle tick should fire now: not mid-response, and past the due time."""
        return not self.busy and time.monotonic() >= self._due


class IdleTicker:
    """Nudges the session into an idle action whenever it has gone quiet.

    Silent while asleep: an idle tick is the robot deciding to do something,
    which is exactly what sleep means it should not do.
    """

    name = "idle"

    def __init__(
        self,
        client: RealtimeClientLike,
        pacer: IdlePacer,
        sleeping: Sleeping,
        idle_prompt: str,
        idle_tools: list[dict[str, Any]],
        log: EventLog,
    ) -> None:
        self._client = client
        self._pacer = pacer
        self._sleeping = sleeping
        self._idle_prompt = idle_prompt
        self._idle_tools = idle_tools
        self._log = log

    async def run(self) -> None:
        while True:
            await asyncio.sleep(_TICK_PERIOD_S)
            if self._sleeping.asleep:
                self._pacer.touch()  # so waking does not fire a tick that was due all along
                continue
            if self._pacer.is_due():
                self._pacer.touch()
                self._log.write("idle_tick")
                await self._client.send(
                    ResponseCreate(
                        instructions=self._idle_prompt,
                        tools=self._idle_tools,
                        output_modalities=["text"],
                        tool_choice="required",
                        metadata=dict(IDLE_TURN_METADATA),
                    )
                )


__all__ = ["IDLE_MAX_SECONDS", "IDLE_MIN_SECONDS", "IDLE_TURN_METADATA", "IdlePacer", "IdleTicker"]
