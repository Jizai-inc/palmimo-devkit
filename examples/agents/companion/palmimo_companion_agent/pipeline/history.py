"""History -- the agent's event log and its translation to LLM messages.

History holds a fixed-length window of typed :data:`Event` objects (rather
than raw LiteLLM/OpenAI message dicts) so each kind can carry whatever payload
the UI or an event log needs, while defining its own :meth:`to_messages` for
the chat format the LLM actually sees.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


#: Number of events the window keeps; older ones are silently evicted.
HISTORY_LEN = 50

#: Gemini rejects a request whose message list opens on an assistant
#: (function_call) turn with INVALID_ARGUMENT -- a function_call turn must
#: immediately follow a user turn or a function-response turn. If the
#: fixed-length window has evicted everything before a ToolExecEvent,
#: to_messages() inserts this user turn ahead of it so that can never happen.
_CONTINUATION_SEED = "[continued] Carry on naturally from what you were doing."


@dataclass(frozen=True)
class MicInputEvent:
    """Speech the microphone (VAD + STT + guard) passed through as an instruction.

    Args:
        directed: See :class:`~.bus.Interrupt`. Only changes the
            ``[speech]``/``[overheard]`` prefix :meth:`to_messages` renders.
    """

    text: str
    directed: bool = True
    kind: Literal["mic_input"] = "mic_input"

    def to_messages(self) -> list[dict]:
        prefix = "[speech]" if self.directed else "[overheard]"
        return [{"role": "user", "content": f"{prefix} {self.text}"}]


@dataclass(frozen=True)
class KeyboardEvent:
    """A typed instruction from the user."""

    text: str
    kind: Literal["keyboard"] = "keyboard"

    def to_messages(self) -> list[dict]:
        return [{"role": "user", "content": f"[instruction] {self.text}"}]


@dataclass(frozen=True)
class CameraEvent:
    """Something the camera saw, described in words.

    Posted by :func:`~palmimo_companion_agent.pipeline.dispatch.describe_images`
    for each image a :class:`ToolExecEvent` result carries (``capture`` and
    the composites that end in one), right after that tool call in the
    window -- so the LLM sees the picture's content on its next turn without
    the chat message list needing image parts of its own.
    """

    summary: str
    kind: Literal["camera"] = "camera"

    def to_messages(self) -> list[dict]:
        return [{"role": "user", "content": f"[vision] {self.summary}"}]


@dataclass(frozen=True)
class SystemNoteEvent:
    """A user-role note the conductor or a turn injects itself (startup, an LLM-call retry, ...)."""

    text: str
    kind: Literal["system_note"] = "system_note"

    def to_messages(self) -> list[dict]:
        return [{"role": "user", "content": self.text}]


@dataclass(frozen=True)
class AgentThoughtEvent:
    """Assistant text from a turn that returned no tool call.

    UI/log-only: :meth:`to_messages` returns nothing. Re-adding a bare
    assistant text turn to the window would risk the same leading-assistant
    problem :data:`_CONTINUATION_SEED` guards against, and
    :class:`ToolExecEvent`'s own ``thought`` field already carries this same
    context back to the LLM on the next tool call, so nothing is lost by
    keeping this UI-only.
    """

    text: str
    kind: Literal["agent_thought"] = "agent_thought"

    def to_messages(self) -> list[dict]:
        return []


@dataclass(frozen=True)
class ToolExecEvent:
    """A tool call the agent made and the result it got back.

    Bundled as one Event (rather than as two separately-added messages) so
    the assistant turn and its tool response can never fall out of the fixed
    window on their own -- both evict together.
    """

    tool_call_id: str
    name: str
    arguments: str  # The raw JSON string the LLM returned.
    result: str
    #: Whether *result* reports a failure -- either the underlying
    #: ToolResult.is_error (a validated call the tool itself rejected or
    #: raised inside), or a local dispatch-side failure (malformed JSON
    #: arguments; see :func:`~.dispatch.execute_and_record`). False (the
    #: default) covers both a normal success and an interrupted call (see
    #: :func:`~.dispatch.is_interrupted_result`) -- interruption is its own,
    #: separate outcome, not an error.
    error: bool = False
    thought: str | None = None
    kind: Literal["tool_exec"] = "tool_exec"

    def to_messages(self) -> list[dict]:
        assistant: dict = {
            "role": "assistant",
            "content": self.thought,
            "tool_calls": [
                {
                    "id": self.tool_call_id,
                    "type": "function",
                    "function": {"name": self.name, "arguments": self.arguments},
                }
            ],
        }
        tool: dict = {"role": "tool", "tool_call_id": self.tool_call_id, "content": self.result}
        return [assistant, tool]


#: kind field discriminated union of every event History can hold.
Event = MicInputEvent | KeyboardEvent | CameraEvent | SystemNoteEvent | AgentThoughtEvent | ToolExecEvent


class History:
    """The agent's fixed-length window of events, with live-update subscribers."""

    def __init__(self, maxlen: int = HISTORY_LEN) -> None:
        self.events: deque[Event] = deque(maxlen=maxlen)
        self._subscribers: list[Callable[[Event], None]] = []

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        """Register *callback* to be invoked with every event as it's added.

        Used for both UI live updates and JSONL event logging (see
        :mod:`palmimo_companion_agent.pipeline.event_log`).
        """
        self._subscribers.append(callback)

    def add(self, event: Event) -> None:
        """Append *event* to the window and notify subscribers.

        A subscriber failure (a closed log stream, a full disk) is logged and
        swallowed: observers are optional, and none of them may take down the
        dialogue loop that is doing the adding.
        """
        self.events.append(event)
        for callback in self._subscribers:
            try:
                callback(event)
            except Exception:
                logging.getLogger(__name__).warning("history subscriber failed; continuing", exc_info=True)

    def to_messages(self) -> list[dict]:
        """Expand the window into LiteLLM/OpenAI chat messages (system prompt excluded).

        See :data:`_CONTINUATION_SEED` for why a leading assistant turn gets
        a seed message inserted ahead of it.
        """
        messages: list[dict] = []
        for event in self.events:
            messages.extend(event.to_messages())
        if messages and messages[0].get("role") == "assistant":
            messages.insert(0, {"role": "user", "content": _CONTINUATION_SEED})
        return messages
