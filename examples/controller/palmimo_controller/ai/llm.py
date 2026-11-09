"""OpenAI speech-to-text and chat, behind the small surface the AI controller needs.

The only module that imports `openai`. Every failure leaves here as an
`LlmError` whose `kind` says whether retrying can help, so the controller
never has to know the SDK's exception tree.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

import openai


_LOG = logging.getLogger(__name__)

#: Defaults; `main.py` lets `CONTROLLER_CHAT_MODEL` / `CONTROLLER_STT_MODEL` override them.
CHAT_MODEL = "gpt-5.4-mini"
STT_MODEL = "gpt-4o-mini-transcribe"
#: The lowest effort every GPT-5 model accepts: a spoken reply cannot wait on long reasoning.
REASONING_EFFORT = "low"

_TIMEOUT_S = 20.0

ErrorKind = Literal["auth", "quota", "model", "transient"]


class LlmError(Exception):
    """A failed OpenAI call. `kind` is `"transient"` when the next turn may well succeed."""

    def __init__(self, kind: ErrorKind, detail: str) -> None:
        super().__init__(detail)
        self.kind: ErrorKind = kind


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ChatReply:
    """One assistant message: what to say, which tools to run, and the message to keep in history."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    message: dict[str, Any] = field(default_factory=dict)


class LlmLike(Protocol):
    """What `AiController` calls. Tests substitute a scripted fake."""

    async def check(self) -> None: ...

    async def transcribe(self, wav: bytes) -> str: ...

    async def chat(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None) -> ChatReply: ...


def _error(exc: Exception) -> LlmError:
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return LlmError("auth", type(exc).__name__)
    if isinstance(exc, openai.RateLimitError):
        if getattr(exc, "code", None) == "insufficient_quota":
            return LlmError("quota", "insufficient_quota")
        return LlmError("transient", "rate_limited")
    if isinstance(exc, openai.NotFoundError):
        return LlmError("model", "model_not_found")
    if isinstance(exc, openai.APIStatusError):
        return LlmError("transient", f"http_{exc.status_code}")
    if isinstance(exc, openai.APIConnectionError):
        return LlmError("transient", "network")
    return LlmError("transient", type(exc).__name__)


class OpenAiLlm:
    """`LlmLike` over the OpenAI API: Chat Completions for replies, the transcription endpoint for STT."""

    def __init__(
        self,
        api_key: str,
        *,
        chat_model: str = CHAT_MODEL,
        stt_model: str = STT_MODEL,
        language: str = "ja",
        reasoning_effort: str | None = REASONING_EFFORT,
    ) -> None:
        self.chat_model = chat_model
        self.stt_model = stt_model
        self._language = language
        self._reasoning_effort = reasoning_effort
        self._client = openai.AsyncOpenAI(api_key=api_key, timeout=_TIMEOUT_S, max_retries=1)

    async def check(self) -> None:
        """Fail fast on a bad key or an unknown model, before the mode goes live."""
        try:
            await self._client.models.retrieve(self.chat_model)
        except Exception as exc:
            raise _error(exc) from exc

    async def transcribe(self, wav: bytes) -> str:
        try:
            result = await self._client.audio.transcriptions.create(
                model=self.stt_model, file=("utterance.wav", wav, "audio/wav"), language=self._language
            )
        except Exception as exc:
            raise _error(exc) from exc
        return (result.text or "").strip()

    async def chat(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None) -> ChatReply:
        options: dict[str, Any] = {}
        if tools:
            options["tools"] = tools
        if self._reasoning_effort:
            options["reasoning_effort"] = self._reasoning_effort
        try:
            # History is kept as plain dicts; the SDK's TypedDict union adds nothing here.
            response = await self._client.chat.completions.create(
                model=self.chat_model, messages=cast(Any, messages), **options
            )
        except Exception as exc:
            raise _error(exc) from exc
        message = response.choices[0].message
        calls = [
            ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments or "{}")
            for call in message.tool_calls or []
            if call.type == "function"
        ]
        kept: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
        if calls:
            kept["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in calls
            ]
        if response.usage is not None:
            _LOG.info(
                "AI: chat used %s",
                json.dumps(response.usage.model_dump(include={"prompt_tokens", "completion_tokens"})),
            )
        return ChatReply(text=(message.content or "").strip(), tool_calls=calls, message=kept)


__all__ = ["CHAT_MODEL", "STT_MODEL", "ChatReply", "LlmError", "LlmLike", "OpenAiLlm", "ToolCall"]
