"""IdleTurn -- one autonomous, speech-free ReAct tick.

Runs while nothing is queued on the bus: a single LLM call over the idle
prompt and the restricted idle :class:`~.toolview.ToolView`
(:data:`~.toolview.IDLE_TOOL_NAMES`), then at most the FIRST tool call the
model picked, dispatched through :func:`~.dispatch.execute_and_record`.
Unlike the respond turn, an idle tick never plans more than one action ahead
-- the :class:`~.conductor.Conductor`'s own pacing (a random pause between
ticks) provides the beat between actions, not a multi-call plan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import dispatch
from .prompt import build_messages, load_prompt


if TYPE_CHECKING:
    from ..core.toolview import ToolView
    from .bus import Bus
    from .history import History
    from .llm import LlmProvider


#: Single-tool wording (unlike respond's up-to-4-plan one) -- see
#: :func:`~.prompt.build_messages` for why this gets appended at all.
_CONTINUATION_NUDGE = "[continued] Pick your next action with a single tool call."

_NO_TOOL_CALL_NOTE = (
    "[notice] That reply wasn't a tool call. Keep any aside as a tool call's `reason`, "
    "and always call exactly one tool to act."
)


class IdleTurn:
    """Drives one autonomous idle tick: chat, then dispatch at most one tool call."""

    def __init__(self, history: History, view: ToolView, llm: LlmProvider, bus: Bus) -> None:
        self.history = history
        self.view = view
        self.llm = llm
        self.bus = bus
        self._system_prompt = load_prompt("idle")

    async def tick(self) -> None:
        """Run one idle tick: a single chat() call, then at most one tool call."""
        messages = build_messages(self.history, self._system_prompt, continuation_nudge=_CONTINUATION_NUDGE)
        try:
            response = await self.llm.chat(messages=messages, tools=self.view.to_openai_tools())
        except Exception as exc:
            await dispatch.record_llm_failure(self.history, exc)
            return
        if not response.choices:
            await dispatch.record_empty_choices(self.history)
            return

        message = response.choices[0].message
        tool_call = message.tool_calls[0] if message.tool_calls else None
        if tool_call is None:
            dispatch.record_no_tool_call(self.history, message.content, no_tool_call_note=_NO_TOOL_CALL_NOTE)
            return

        await dispatch.execute_and_record(
            self.view, self.bus, self.llm, self.history, tool_call, thought=message.content
        )


__all__ = ["IdleTurn"]
