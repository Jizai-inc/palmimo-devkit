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
#: Low: a spoken reply cannot wait on long reasoning.
REASONING_EFFORT: str | None = "low"

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
    """One model response: what to say, which tools to run, and the output items to keep in history."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)


class LlmLike(Protocol):
    """What `AiController` calls. Tests substitute a scripted fake."""

    async def check(self) -> None: ...

    async def transcribe(self, wav: bytes) -> str: ...

    async def chat(self, items: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None) -> ChatReply: ...


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
        # The status code alone cannot tell a bad request apart from an outage.
        _LOG.warning("OpenAI returned HTTP %s: %s", exc.status_code, exc.message)
        return LlmError("transient", f"http_{exc.status_code}")
    if isinstance(exc, openai.APIConnectionError):
        return LlmError("transient", "network")
    return LlmError("transient", type(exc).__name__)


class OpenAiLlm:
    """`LlmLike` over the OpenAI API: the Responses API for replies, the transcription endpoint for STT."""

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

    async def chat(self, items: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None) -> ChatReply:
        options: dict[str, Any] = {}
        if tools:
            options["tools"] = tools
        if self._reasoning_effort:
            options["reasoning"] = {"effort": self._reasoning_effort}
        try:
            # store=False keeps visitors' words off OpenAI's servers, so the
            # whole conversation is resent each turn. Reasoning items then have
            # to carry their encrypted content to be passed back with tool calls.
            response = await self._client.responses.create(
                model=self.chat_model,
                input=cast(Any, items),
                store=False,
                include=["reasoning.encrypted_content"],
                **options,
            )
        except Exception as exc:
            raise _error(exc) from exc
        calls = [
            ToolCall(id=item.call_id, name=item.name, arguments=item.arguments or "{}")
            for item in response.output
            if item.type == "function_call"
        ]
        kept = [item.model_dump(mode="json", exclude_none=True) for item in response.output]
        if response.usage is not None:
            _LOG.info(
                "AI: chat used %s",
                json.dumps(response.usage.model_dump(include={"input_tokens", "output_tokens"})),
            )
        return ChatReply(text=response.output_text.strip(), tool_calls=calls, items=kept)


__all__ = ["CHAT_MODEL", "STT_MODEL", "ChatReply", "LlmError", "LlmLike", "OpenAiLlm", "ToolCall"]
