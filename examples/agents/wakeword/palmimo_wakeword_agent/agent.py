"""Single-round LLM tool-calling agent over the Palmimo SDK's ``AgentToolSet``.

Deliberately simple: no autonomous/ReAct loop. One command in, at most one
round of tool calls, one spoken reply out.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol


DEFAULT_SYSTEM_PROMPT = (
    "You are Palmimo, a palm-sized hexapod robot. "
    "Execute the user's spoken command using the provided tools, then reply "
    "with one short spoken sentence."
)

_FALLBACK_REPLY = "Done."


class _ToolResultLike(Protocol):
    text: str


class _ToolSetLike(Protocol):
    def to_openai_tools(self) -> list[dict[str, Any]]: ...

    async def call(self, name: str, args: dict[str, Any] | str) -> _ToolResultLike: ...


# All data members below are read-only properties, not plain attributes: a
# plain protocol attribute demands an INVARIANT match (a fake exposing
# `list[_ToolCall]` would not satisfy `Sequence[_ToolCallLike]`), while a
# read-only property only needs covariant read access — which both the real
# openai response models and lightweight test fakes provide.
class _FunctionCallLike(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def arguments(self) -> str: ...


class _ToolCallLike(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def function(self) -> _FunctionCallLike: ...


class _ChatMessageLike(Protocol):
    @property
    def content(self) -> str | None: ...
    @property
    def tool_calls(self) -> Sequence[_ToolCallLike] | None: ...

    def model_dump(self) -> dict[str, Any]: ...


class _ChoiceLike(Protocol):
    @property
    def message(self) -> _ChatMessageLike: ...


class _CompletionLike(Protocol):
    @property
    def choices(self) -> Sequence[_ChoiceLike]: ...


class _CompletionsLike(Protocol):
    def create(
        self,
        *,
        model: str,
        # `messages` and `tools` are loosened to `Any` (rather than `Sequence[dict[str,
        # Any]]`): the real `openai.OpenAI` client's `create()` expects
        # `Iterable[ChatCompletionMessageParam]` / `Iterable[ChatCompletionToolUnionParam] |
        # Omit`, both TypedDict-based unions that don't structurally match our plain-dict
        # shape (or accept `None`) under mypy. Everything else stays precise; see the module
        # docstring risk note.
        messages: Any,
        tools: Any = ...,
    ) -> _CompletionLike: ...


class _ChatLike(Protocol):
    @property
    def completions(self) -> _CompletionsLike: ...


class ChatClientLike(Protocol):
    """Minimal structural surface of an ``openai.OpenAI``-compatible client we depend on."""

    @property
    def chat(self) -> _ChatLike: ...


class CommandAgent:
    """Executes one spoken command via tool-calling, then replies.

    Args:
        client: An ``openai.OpenAI``-compatible object (dependency-injected
            for tests).
        toolset: The registered :class:`~palmimo_sdk.agent.AgentToolSet` (or
            a duck-typed equivalent) to execute tool calls against.
        model: Chat completion model name. A plain default arg (not read from
            an environment variable here) so ``main()`` can override it.
        system_prompt: System message steering the model's persona and reply
            style.
        language: ISO 639-1 language code (e.g. "en", "ja") the reply should be
            spoken in. Appended to *system_prompt* as a deterministic
            instruction suffix -- LLMs handle ISO codes fine, so no
            langcode-to-name translation table is needed.
    """

    def __init__(
        self,
        client: ChatClientLike,
        toolset: _ToolSetLike,
        model: str = "gemini-3.5-flash-lite",
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        language: str = "en",
    ) -> None:
        self._client = client
        self._toolset = toolset
        self._model = model
        self._system_prompt = f"{system_prompt} Reply in language: {language}."

    async def run(self, text: str) -> str:
        """Execute *text* as a spoken command; return the spoken reply.

        One round of tool calls at most: the first completion may return
        tool calls, each of which is executed (in order, awaited one at a
        time -- ``toolset.call`` is async, but
        this agent still runs the resulting tool-calling protocol as a
        strictly one-at-a-time sequence, matching the one-round contract
        above) via ``toolset.call``; a single follow-up completion (without
        ``tools=``) then turns the tool results into the spoken reply. If the
        first completion returns no tool calls, its own text is the reply.
        The ``openai`` client calls themselves stay synchronous (blocking)
        network calls -- only the robot-driving ``toolset.call`` needs to be
        awaited here.
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": text},
        ]
        first = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=self._toolset.to_openai_tools(),
        )
        message = first.choices[0].message
        tool_calls = message.tool_calls
        if not tool_calls:
            return self._clean_reply(message.content)

        messages.append(message.model_dump())
        for tool_call in tool_calls:
            try:
                result = await self._toolset.call(tool_call.function.name, tool_call.function.arguments)
                result_text = result.text
            except Exception as exc:  # a bad tool call must not crash the voice loop
                result_text = f"Error while executing tool {tool_call.function.name!r}: {exc}"
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": result_text})

        follow_up = self._client.chat.completions.create(model=self._model, messages=messages)
        return self._clean_reply(follow_up.choices[0].message.content)

    @staticmethod
    def _clean_reply(content: str | None) -> str:
        text = (content or "").strip()
        return text if text else _FALLBACK_REPLY
