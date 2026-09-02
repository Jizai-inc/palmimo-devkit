"""A thin LiteLLM aggregate -- chat / VLM / STT / guard behind one provider.

Design notes:
  - LiteLLM itself is not hidden: ``chat`` returns the ``ModelResponse``
    as-is, and the caller consumes ``tool_calls`` / ``content`` directly.
  - Each capability (chat / vlm / stt / guard) has its own independent model
    string, so they can point at different providers.
  - An unconfigured capability raises :class:`RuntimeError` explicitly --
    there is no silent fallback.
"""

from __future__ import annotations

import io
import logging
import re
from functools import cache
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast

import litellm
from litellm import ModelResponse
from pydantic import BaseModel, Field, ValidationError


if TYPE_CHECKING:
    from .settings import PipelineSettings


_log = logging.getLogger(__name__)

#: Sentinel distinguishing "the response has no text field at all" (a
#: malformed/unexpected response shape) from "the field is present and holds
#: None/empty" (a legitimate empty transcription of silence).
_MISSING = object()

#: Gemini flash-lite's 503 "high demand" errors are model-specific congestion;
#: retrying on a different model in the same family tends to succeed. Kept as
#: a module constant rather than a setting -- this is an implementation
#: detail of working around one provider's flakiness, not something a user
#: needs to tune.
_CHAT_FALLBACK_MODEL = "gemini/gemini-3.5-flash"

_CHAT_TIMEOUT_SECONDS = 60.0
_VLM_TIMEOUT_SECONDS = 60.0
_STT_TIMEOUT_SECONDS = 30.0
_GUARD_TIMEOUT_SECONDS = 15.0
_CHAT_NUM_RETRIES = 1
_GUARD_NUM_RETRIES = 1

#: Exception types that mean "the provider is temporarily overloaded", not
#: "something is wrong with this request". Only these are eligible for the
#: chat fallback below -- an auth failure, a bad request, or a context-window
#: overflow is a configuration/usage problem the fallback model would have
#: just as much, so masking it behind a second call would only hide the real
#: error and delay fixing it.
_TRANSIENT_CHAT_EXCEPTIONS: tuple[type[Exception], ...] = (
    litellm.exceptions.ServiceUnavailableError,
    litellm.exceptions.InternalServerError,
    litellm.exceptions.RateLimitError,
    litellm.exceptions.Timeout,
)


class SpeechVerdict(BaseModel):
    """The guard's structured classification output.

    ``kind`` is one of four categories:
      - ``"noise"``: hallucinated/garbled STT output, a filler word, or a
        meaningless fragment. ``text`` is always None.
      - ``"command"``: an instruction, greeting, or address directed at the
        robot.
      - ``"question"``: a question directed at the robot.
      - ``"ambient"``: not directed at the robot -- ambient conversation or a
        stray remark.
    """

    kind: Literal["noise", "command", "question", "ambient"] = Field(
        description=(
            "Speech input classification.\n"
            'noise    -- hallucinated/garbled transcription, a filler word ("uh", "um", '
            '"mm-hmm"), or a meaningless fragment.\n'
            "command  -- an instruction or greeting directed at the robot "
            '("dance", "look over there", "stop", "hello").\n'
            "question -- a question directed at the robot, ending in a clear question form.\n"
            "ambient  -- not directed at the robot: ambient conversation or a stray remark whose "
            'meaning is otherwise clear ("I\'m hungry", "I\'d love a burger", "work\'s been busy '
            "lately\"). A statement that doesn't end in a question form belongs here, not in "
            "question.\n"
            "When in doubt, avoid noise (lean toward classifying anything with recognizable "
            "meaning or topic)."
        )
    )
    text: str | None = Field(
        default=None,
        description=(
            "When kind is not noise, the raw transcription with noise/hallucination stripped out "
            "(pass it through unchanged if it is already clean). Null when kind is noise."
        ),
    )


_SPEECH_CLASSIFY_SYSTEM_PROMPT = (
    "You are a speech-input classifier for a companion robot. Classify each transcribed "
    "utterance into exactly one of four categories.\n"
    "\n"
    "noise -- clearly hallucinated or garbled transcription output, or a meaningless fragment. "
    'Filler words / back-channel responses ("uh", "um", "mm-hmm", "yeah") count as noise. '
    "Everything else leans toward one of the other three categories.\n"
    "\n"
    "command -- an instruction, greeting, or address directed at the robot (an action verb, an "
    'imperative, or a greeting like "hello").\n'
    "\n"
    "question -- a question directed at the robot, ending in a clear question form. Whether it "
    "can be answered yes/no is irrelevant to this classification (a factual question the robot "
    "cannot know the answer to is still a question; set text as usual, no separate handling "
    "needed). A statement that does NOT end in a question form is not a question, even if it "
    "raises a topic -- classify it as ambient instead.\n"
    "\n"
    "ambient -- not directed at the robot: ambient conversation, or a stray remark whose meaning "
    "is clear.\n"
    "\n"
    "When in doubt, avoid noise -- lean toward one of the other three categories whenever the "
    "utterance carries any recognizable meaning or topic. text: for any kind other than noise, "
    "the raw transcription with noise/hallucination stripped out (pass through unchanged if "
    "already clean); null for noise."
)


# Early-reject heuristics, applied before the guard LLM is called at all (a
# guard_model call costs tokens and latency the classifier for these obvious
# cases doesn't need to pay).

#: Bare back-channel responses a transcriber commonly hallucinates from
#: silence/breath noise.
_NOISE_BACKCHANNELS: frozenset[str] = frozenset(
    {"はい", "うん", "ええ", "あー", "えー", "おー", "ねえ", "そう", "うわ", "おお", "わー", "ん", "あ", "え", "お"}
)

#: Fixed phrases a transcriber hallucinates from silence/music/applause (a
#: video sign-off, learned from its training data) -- never something a user
#: actually says to the robot, so matched as a substring and dropped outright.
_NOISE_HALLUCINATION_PHRASES: tuple[str, ...] = (
    "ご視聴ありがとうございました",
    "ご清聴ありがとうございました",
    "最後までご視聴",
    "チャンネル登録",
    "thanks for watching",
    "please subscribe",
)

#: Below this length, a fragment is almost always a transcription artifact
#: rather than real speech (a real instruction like "stop" still clears it).
_MIN_COMMAND_LEN = 3

#: Scripts never used in Japanese or English input (Cyrillic, Hebrew, Arabic,
#: Devanagari, Thai, Hangul) -- a multi-language transcriber occasionally
#: hallucinates these from silence or crowd noise.
_FOREIGN_SCRIPT_RE = re.compile("[Ѐ-ӿ֐-׿؀-ۿऀ-ॿ฀-๿ᄀ-ᇿ㄰-㆏가-힣]")


def _same_provider(model_a: str, model_b: str) -> bool:
    """Whether two LiteLLM model strings ("<provider>/<name>") name the same provider.

    Used by :meth:`LlmProvider.chat` to gate the fallback to only when the
    primary model is already ``gemini/...`` (the same provider as
    :data:`_CHAT_FALLBACK_MODEL`): a cross-provider fallback (e.g. a
    self-hosted model quietly falling back to a cloud one) would send data to
    a provider the deployment never opted into.
    """
    return model_a.split("/", 1)[0] == model_b.split("/", 1)[0]


@cache
def _accepts_parallel_tool_calls(model: str) -> bool:
    """Whether *model*'s provider accepts the ``parallel_tool_calls`` parameter.

    LiteLLM rejects a parameter the provider does not support
    (``UnsupportedParamsError``) rather than dropping it, so sending
    ``parallel_tool_calls`` unconditionally fails *every* completion against a
    provider that lacks it -- Ollama and other self-hosted backends among them.
    Asking LiteLLM which parameters the provider supports keeps the parameter
    where it is honoured and omits it only where it would be rejected.

    Preferred over ``drop_params``, LiteLLM's own remedy for this error, in
    either its global (``litellm.drop_params = True``) or per-call
    (``drop_params=True`` among the completion arguments) form. Scope is not
    the objection: the per-call form applies to one call only and would leave
    the ``response_format`` carrying the guard's schema in
    :meth:`LlmProvider.classify_speech` untouched. The objection is that it
    silences every parameter mismatch at once, and against a self-hosted
    backend one of those complaints is the only warning the deployment gets.
    LiteLLM's parameter table for ``ollama/...`` -- the plain route, as
    opposed to the tool-calling ``ollama_chat/...`` one -- has no ``tools``
    entry, and the tool definitions are discarded on the way out whether or
    not ``drop_params`` is set; the ``parallel_tool_calls`` error is what
    stops the call and makes the wrong route visible. Silence it and the
    agent starts, talks, and never acts on a single tool call. Querying the
    supported parameters settles one named parameter instead, and leaves
    every other mismatch as loud as it was.

    An unrecognized model string counts as "does not accept": the completion
    call that follows reports the real problem, and a lookup miss here should
    not become an exception of its own. The answer is fixed for a given model
    string, so it is cached for the process.
    """
    try:
        supported = litellm.get_supported_openai_params(model=model)
    except Exception as exc:
        _log.debug("could not determine supported params for %s, omitting parallel_tool_calls: %s", model, exc)
        return False
    return supported is not None and "parallel_tool_calls" in supported


class _ChatKwargs(TypedDict):
    """One chat ``acompletion`` call's keyword arguments.

    Spelled out as a TypedDict rather than ``dict[str, Any]`` so that mypy
    checks the assembled call where :func:`_chat_kwargs` builds it: a
    misspelled key or a wrong-typed value is an error there, whereas
    ``dict[str, Any]`` accepts anything. Note that this is the only place the
    call *can* be checked -- LiteLLM exports ``acompletion`` through a
    module-level ``__getattr__``, so the function itself is ``Any`` to mypy
    and the unpacking at the call site verifies nothing either way.
    """

    model: str
    max_tokens: int
    tools: list[dict] | None
    messages: list[dict]
    timeout: float
    num_retries: int
    metadata: dict[str, str]
    #: Present only for a provider that accepts it -- see
    #: :func:`_accepts_parallel_tool_calls`.
    parallel_tool_calls: NotRequired[bool]


def _chat_kwargs(
    model: str,
    *,
    messages: list[dict],
    tools: list[dict] | None,
    parallel_tool_calls: bool,
    max_tokens: int,
    num_retries: int,
) -> _ChatKwargs:
    """Assemble one chat ``acompletion`` call's keyword arguments.

    ``parallel_tool_calls`` is included only for a provider that accepts it
    (see :func:`_accepts_parallel_tool_calls`). Omitting it leaves the
    provider's own default in force, which is safe here because neither caller
    relies on the parameter to bound a plan: the idle turn takes at most the
    first tool call and the respond turn truncates to its own maximum, both in
    code.
    """
    kwargs: _ChatKwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "tools": tools,
        "messages": messages,
        "timeout": _CHAT_TIMEOUT_SECONDS,
        "num_retries": num_retries,
        "metadata": {"companion_role": "chat"},
    }
    if _accepts_parallel_tool_calls(model):
        kwargs["parallel_tool_calls"] = parallel_tool_calls
    return kwargs


class LlmProvider:
    """The companion agent's LiteLLM capabilities, gathered behind one object.

    Args:
        chat_model: LiteLLM model string for the tool-calling chat loop.
        vlm_model: LiteLLM model string for image description. None disables
            :meth:`describe_image`.
        stt_model: LiteLLM model string for speech-to-text. None disables
            :meth:`transcribe`.
        guard_model: LiteLLM model string for the speech-classification
            guard. None makes :meth:`classify_speech` pass everything
            through as ``"command"``.
    """

    def __init__(
        self,
        *,
        chat_model: str,
        vlm_model: str | None = None,
        stt_model: str | None = None,
        guard_model: str | None = None,
    ) -> None:
        self.chat_model = chat_model
        self.vlm_model = vlm_model
        self.stt_model = stt_model
        self.guard_model = guard_model

    @classmethod
    def from_settings(cls, settings: PipelineSettings) -> LlmProvider:
        """Build a provider from a :class:`PipelineSettings` instance."""
        return cls(
            chat_model=settings.chat_model,
            vlm_model=settings.vlm_model,
            stt_model=settings.stt_model,
            # Empty disables the guard: one fewer model call between
            # hearing something and answering it.
            guard_model=settings.guard_model or None,
        )

    # ------------------------------------------------------------------
    # chat -- called from the agent's tool-calling loop
    # ------------------------------------------------------------------
    async def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        parallel_tool_calls: bool = False,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        """Wrap LiteLLM's acompletion and return the ModelResponse.

        The caller consumes ``resp.choices[0].message``'s ``content`` /
        ``tool_calls`` directly (LiteLLM's own shape, unwrapped).

        ``parallel_tool_calls`` reaches the provider only when the provider
        accepts it -- see :func:`_accepts_parallel_tool_calls` and
        :func:`_chat_kwargs`.

        If the primary model fails (after ``_CHAT_NUM_RETRIES`` retries) with
        one of :data:`_TRANSIENT_CHAT_EXCEPTIONS` -- congestion, not a
        configuration or usage problem -- and the primary model is on the
        same provider as :data:`_CHAT_FALLBACK_MODEL` (``gemini/...``), this
        retries once, without further retries, against
        :data:`_CHAT_FALLBACK_MODEL`: gemini flash-lite's 503 "high
        demand" errors are model-specific congestion, and a different model
        in the same family tends to succeed.

        The fallback is deliberately narrow. Falling back on *any* exception
        would (a) mask a misconfigured deployment (a bad API key, an invalid
        request, a context-window overflow) behind a second call that fails
        the same way or "succeeds" against the wrong model, hiding the real
        error, and (b) silently route a non-Gemini deployment's data to
        Google's API, which the deployment never opted into. Everything else
        -- auth errors, bad requests, context-length errors, and transient
        errors on a non-Gemini primary model -- propagates as-is. If the
        fallback call itself fails, that exception also propagates.
        """
        try:
            return cast(
                ModelResponse,
                await litellm.acompletion(
                    **_chat_kwargs(
                        self.chat_model,
                        messages=messages,
                        tools=tools,
                        parallel_tool_calls=parallel_tool_calls,
                        max_tokens=max_tokens,
                        num_retries=_CHAT_NUM_RETRIES,
                    )
                ),
            )
        except _TRANSIENT_CHAT_EXCEPTIONS as exc:
            if not _same_provider(self.chat_model, _CHAT_FALLBACK_MODEL):
                raise
            _log.warning("chat model %s failed, falling back to %s: %s", self.chat_model, _CHAT_FALLBACK_MODEL, exc)
            return cast(
                ModelResponse,
                await litellm.acompletion(
                    **_chat_kwargs(
                        _CHAT_FALLBACK_MODEL,
                        messages=messages,
                        tools=tools,
                        parallel_tool_calls=parallel_tool_calls,
                        max_tokens=max_tokens,
                        num_retries=0,
                    )
                ),
            )

    # ------------------------------------------------------------------
    # VLM -- called from the capture tool
    # ------------------------------------------------------------------
    async def describe_image(self, image_b64: str, prompt: str, *, max_tokens: int = 200) -> str:
        """Send an image + prompt to the VLM and return its text description.

        Raises:
            RuntimeError: ``vlm_model`` is not configured.
        """
        if self.vlm_model is None:
            raise RuntimeError("VLM model is not configured (set COMPANION_AGENT_VLM_MODEL)")
        resp = cast(
            ModelResponse,
            await litellm.acompletion(
                model=self.vlm_model,
                max_tokens=max_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                        ],
                    }
                ],
                timeout=_VLM_TIMEOUT_SECONDS,
                metadata={"companion_role": "vlm"},
            ),
        )
        return resp.choices[0].message.content or "(the VLM returned an empty response)"

    # ------------------------------------------------------------------
    # STT -- called from the microphone peripheral (a later phase)
    # ------------------------------------------------------------------
    async def transcribe(self, audio_bytes: bytes, *, language: str = "ja", filename: str = "audio.wav") -> str:
        """Send audio bytes to the STT model and return the transcription.

        Args:
            audio_bytes: WAV (or similar) audio, passed straight to
                LiteLLM's atranscription.
            language: ISO 639-1 language hint.
            filename: Name attached to the file-like object; some providers
                infer content type from the extension.

        Raises:
            RuntimeError: ``stt_model`` is not configured, or the response
                has no ``text`` field at all (a malformed/unexpected
                response shape, distinct from a legitimate empty
                transcription of silence).
        """
        if self.stt_model is None:
            raise RuntimeError("STT model is not configured (set COMPANION_AGENT_STT_MODEL)")
        buf = io.BytesIO(audio_bytes)
        buf.name = filename
        resp = await litellm.atranscription(
            model=self.stt_model,
            file=buf,
            language=language,
            timeout=_STT_TIMEOUT_SECONDS,
            metadata={"companion_role": "stt"},
        )
        raw = resp.get("text", _MISSING) if isinstance(resp, dict) else getattr(resp, "text", _MISSING)
        if raw is _MISSING:
            raise RuntimeError(f"STT response has no 'text' field: {resp!r}")
        text = cast(str | None, raw)
        return (text or "").strip()

    # ------------------------------------------------------------------
    # guard -- classifies raw STT output before it reaches the chat loop
    # ------------------------------------------------------------------
    async def classify_speech(self, text: str) -> SpeechVerdict:
        """Classify raw STT text into noise/command/question/ambient.

        If ``guard_model`` is not configured, this skips classification and
        returns the input as-is with ``kind="command"`` (pass-through).

        Python-level early rejection (known filler words, known
        hallucination phrases, too-short fragments, foreign scripts) returns
        ``kind="noise"`` without calling the guard model at all.

        Raises:
            Exception: the guard call itself fails (timeout / network /
                provider error), or its response doesn't match the schema.
                Callers are expected to fall back to treating the input as a
                command on failure -- classification failing should not
                silently drop real speech, so this method does not swallow it.
        """
        stripped = text.strip()
        if not stripped:
            _log.info("guard early-reject (%s): %r", "empty", stripped)
            return SpeechVerdict(kind="noise", text=None)
        if stripped in _NOISE_BACKCHANNELS:
            _log.info("guard early-reject (%s): %r", "backchannel", stripped)
            return SpeechVerdict(kind="noise", text=None)
        if any(phrase in stripped for phrase in _NOISE_HALLUCINATION_PHRASES):
            _log.info("guard early-reject (%s): %r", "hallucination-phrase", stripped)
            return SpeechVerdict(kind="noise", text=None)
        if _FOREIGN_SCRIPT_RE.search(stripped):
            _log.info("guard early-reject (%s): %r", "foreign-script", stripped)
            return SpeechVerdict(kind="noise", text=None)
        if len(stripped) < _MIN_COMMAND_LEN:
            _log.info("guard early-reject (%s): %r", "too-short", stripped)
            return SpeechVerdict(kind="noise", text=None)
        if self.guard_model is None:
            return SpeechVerdict(kind="command", text=stripped)  # pass-through

        resp = cast(
            ModelResponse,
            await litellm.acompletion(
                model=self.guard_model,
                max_tokens=120,
                response_format=SpeechVerdict,
                messages=[
                    {"role": "system", "content": _SPEECH_CLASSIFY_SYSTEM_PROMPT},
                    {"role": "user", "content": stripped},
                ],
                timeout=_GUARD_TIMEOUT_SECONDS,
                num_retries=_GUARD_NUM_RETRIES,
                metadata={"companion_role": "guard"},
            ),
        )

        content = resp.choices[0].message.content or ""
        try:
            verdict = SpeechVerdict.model_validate_json(content)
        except ValidationError as exc:
            raise RuntimeError(f"guard model returned a response that doesn't match the schema: {content!r}") from exc

        if verdict.kind == "noise":
            _log.info("guard verdict noise: %r", stripped)
            return SpeechVerdict(kind="noise", text=None)

        normalized = verdict.text.strip() if verdict.text and verdict.text.strip() else stripped
        _log.info("guard verdict %s: %r (cleaned: %r)", verdict.kind, stripped, normalized)
        return SpeechVerdict(kind=verdict.kind, text=normalized)
