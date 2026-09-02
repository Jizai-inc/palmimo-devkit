"""RespondTurn -- one event-driven reply to speech/keyboard input, as a single planned tool-call sequence.

A single LLM call (``parallel_tool_calls=True``, the full
:class:`~.toolview.ToolView`) returns up to 4 tool calls, executed
**sequentially** via :func:`~.dispatch.execute_and_record`. No follow-up
chat call within one turn -- an observation-dependent question the model
can't answer from the plan alone is an accepted limitation, traded for a
bounded, predictable turn. A turn triggered by directed speech gets an
extra nudge appended to its chat() call -- see :data:`_DIRECTED_SPEECH_NUDGE`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import dispatch
from .history import AgentThoughtEvent, SystemNoteEvent
from .prompt import build_messages, load_prompt


if TYPE_CHECKING:
    from ..core.toolview import ToolView
    from .bus import Bus
    from .dispatch import ToolCallLike
    from .history import History
    from .llm import LlmProvider


#: States the up-to-4-calls contract (unlike idle's single-tool wording,
#: which would contradict it here) -- see :func:`~.prompt.build_messages`
#: for why this gets appended at all.
_CONTINUATION_NUDGE = "[continued] Pick your next action -- one plan of up to 4 tool calls."

#: A respond turn plans at most this many tool calls; anything the model
#: returns beyond it is discarded with a note rather than executed.
_MAX_TOOL_CALLS = 4

_NO_TOOL_CALL_NOTE = (
    "[notice] That reply wasn't a tool call. Keep any aside as a tool call's `reason`, and "
    "reply by calling tools -- one or several (up to 4) -- to act."
)

#: respond.md already states that a [speech] turn must speak, but real runs
#: against gemini flash-lite showed it dropping that rule under a 4-call
#: plan -- this is a deterministic backstop, appended (never stored in
#: history) only to a turn triggered by directed speech. Japanese, unlike
#: this module's other notes: LLM-facing text for a Japanese-speaking
#: character (persona.md), and empirically the wording that moved the
#: needle on flash-lite -- an English instruction tested less reliably.
_DIRECTED_SPEECH_NUDGE = (
    "[notice] たった今声で話しかけられました。このプランには必ず発話（いずれかの tool の "
    "say 引数、または say tool）を含めてください。"
)


class RespondTurn:
    """Drives one respond turn: a single chat() call, then its planned tool calls in order.

    Args:
        view: The full (unrestricted) :class:`~.toolview.ToolView` -- compare
            :class:`~.idle.IdleTurn`'s restricted one.
    """

    def __init__(self, history: History, view: ToolView, llm: LlmProvider, bus: Bus) -> None:
        self.history = history
        self.view = view
        self.llm = llm
        self.bus = bus
        self._system_prompt = load_prompt("respond")

    async def run(self, *, directed_speech: bool = False) -> None:
        """Run one respond turn: chat() once, then execute its plan in order.

        Args:
            directed_speech: Whether this batch included speech addressed to
                the robot (vs. keyboard-only or ambient) -- see
                :data:`_DIRECTED_SPEECH_NUDGE`.
        """
        messages = build_messages(self.history, self._system_prompt, continuation_nudge=_CONTINUATION_NUDGE)
        if directed_speech:
            messages.append({"role": "user", "content": _DIRECTED_SPEECH_NUDGE})
        try:
            response = await self.llm.chat(
                messages=messages, tools=self.view.to_openai_tools(), parallel_tool_calls=True
            )
        except Exception as exc:
            await dispatch.record_llm_failure(self.history, exc)
            return
        if not response.choices:
            await dispatch.record_empty_choices(self.history)
            return

        message = response.choices[0].message
        tool_calls = list(message.tool_calls) if message.tool_calls else []
        if not tool_calls:
            dispatch.record_no_tool_call(self.history, message.content, no_tool_call_note=_NO_TOOL_CALL_NOTE)
            return

        truncated = len(tool_calls) > _MAX_TOOL_CALLS
        planned = tool_calls[:_MAX_TOOL_CALLS]
        if truncated:
            self.history.add(
                SystemNoteEvent(
                    f"[notice] the plan named {len(tool_calls)} tool calls; only the first "
                    f"{_MAX_TOOL_CALLS} were executed."
                )
            )

        # A plan can carry BOTH assistant content and tool_calls, with no
        # single call the content obviously belongs to -- record it once,
        # up front, as its own AgentThoughtEvent (UI/log-only) rather than
        # attaching it to every call's `thought`.
        if message.content and message.content.strip():
            self.history.add(AgentThoughtEvent(message.content.strip()))

        for index, tool_call in enumerate(planned):
            # Checked before EVERY call, including the first: an interrupt
            # may have arrived while the LLM call that produced this plan
            # was still in flight (case a) -- the whole plan is then stale,
            # so nothing in it runs. The flag is deliberately left set here
            # (never cleared) -- the conductor's own drain owns consuming it
            # on its next iteration; clearing it here would let the interrupt
            # that made this plan stale slip past that drain unseen.
            if self.bus.cancel.is_set():
                self._discard_remaining(len(planned) - index)
                return
            interrupted = await self._execute_one(tool_call)
            if interrupted:
                # A long-running call lost a cancel race mid-flight (case b):
                # dispatch.run_tool's own cancellation path already recorded
                # this call as "interrupted: ..." and cleared bus.cancel
                # itself, so the pre-call check above would never catch the
                # rest of a stale plan on the next iteration -- this explicit
                # check does instead.
                self._discard_remaining(len(planned) - index - 1)
                return

    def _discard_remaining(self, remaining: int) -> None:
        """Record that *remaining* still-planned tool calls were dropped, without running them."""
        if remaining <= 0:
            return
        self.history.add(
            SystemNoteEvent(f"[notice] remaining {remaining} planned tool call(s) discarded (new input arrived)")
        )

    async def _execute_one(self, tool_call: ToolCallLike) -> bool:
        """Execute one planned tool call (``thought=None`` -- see :func:`~.dispatch.execute_and_record`)."""
        return await dispatch.execute_and_record(self.view, self.bus, self.llm, self.history, tool_call, thought=None)


__all__ = ["RespondTurn"]
