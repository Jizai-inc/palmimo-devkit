"""Tests for :mod:`palmimo_companion_agent.pipeline.llm` (no real network calls; litellm is monkeypatched)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import litellm
import pytest

from palmimo_companion_agent.pipeline.llm import (
    _CHAT_FALLBACK_MODEL,
    LlmProvider,
    SpeechVerdict,
    _accepts_parallel_tool_calls,
)


class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeMessage(content)


class _FakeModelResponse:
    def __init__(self, content: str | None) -> None:
        self.choices = [_FakeChoice(content)]


def _transient_error() -> Exception:
    """A litellm exception type eligible for the chat fallback (see :data:`_TRANSIENT_CHAT_EXCEPTIONS`)."""
    return litellm.exceptions.ServiceUnavailableError(
        message="simulated overload", llm_provider="gemini", model="gemini/gemini-3.5-flash-lite"
    )


class _RecordingAcompletion:
    """Stand-in for ``litellm.acompletion`` that records calls and can fail on demand."""

    def __init__(self, *, content: str = "ok", fail_times: int = 0, error: Exception | None = None) -> None:
        self.content = content
        self.fail_times = fail_times
        self.error = error if error is not None else _transient_error()
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> _FakeModelResponse:
        self.calls.append(kwargs)
        if len(self.calls) <= self.fail_times:
            raise self.error
        return _FakeModelResponse(self.content)


@pytest.fixture(autouse=True)
def _clear_supported_params_cache() -> Iterator[None]:
    """Drop the per-model support lookup's cache so tests don't observe each other's answers."""
    _accepts_parallel_tool_calls.cache_clear()
    yield
    _accepts_parallel_tool_calls.cache_clear()


@pytest.fixture
def provider() -> LlmProvider:
    return LlmProvider(
        chat_model="gemini/gemini-3.5-flash-lite",
        guard_model="gemini/gemini-3.5-flash-lite",
        vlm_model="gemini/gemini-3.5-flash-lite",
        stt_model="openai/gpt-4o-mini-transcribe",
    )


class TestChat:
    async def test_chat_returns_the_response(self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _RecordingAcompletion(content="hello")
        monkeypatch.setattr("litellm.acompletion", fake)

        resp = await provider.chat([{"role": "user", "content": "hi"}])

        assert resp.choices[0].message.content == "hello"
        assert fake.calls[0]["model"] == "gemini/gemini-3.5-flash-lite"

    async def test_chat_forwards_tools(self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _RecordingAcompletion()
        monkeypatch.setattr("litellm.acompletion", fake)
        tools = [{"type": "function", "function": {"name": "forward"}}]

        await provider.chat([{"role": "user", "content": "hi"}], tools=tools)

        assert fake.calls[0]["tools"] == tools

    async def test_chat_falls_back_to_the_fallback_model_on_a_transient_failure(
        self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _RecordingAcompletion(content="from-fallback", fail_times=1)
        monkeypatch.setattr("litellm.acompletion", fake)

        resp = await provider.chat([{"role": "user", "content": "hi"}])

        assert resp.choices[0].message.content == "from-fallback"
        assert len(fake.calls) == 2
        assert fake.calls[0]["model"] == "gemini/gemini-3.5-flash-lite"
        assert fake.calls[1]["model"] == _CHAT_FALLBACK_MODEL

    async def test_chat_raises_if_the_fallback_also_fails(
        self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _RecordingAcompletion(fail_times=100)
        monkeypatch.setattr("litellm.acompletion", fake)

        with pytest.raises(litellm.exceptions.ServiceUnavailableError):
            await provider.chat([{"role": "user", "content": "hi"}])

    async def test_chat_does_not_fall_back_on_a_non_transient_failure(
        self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An auth/bad-request/context-overflow error is a real problem the fallback would share, so it propagates."""
        fake = _RecordingAcompletion(
            error=litellm.exceptions.AuthenticationError(
                message="bad api key", llm_provider="gemini", model="gemini/gemini-3.5-flash-lite"
            ),
            fail_times=100,
        )
        monkeypatch.setattr("litellm.acompletion", fake)

        with pytest.raises(litellm.exceptions.AuthenticationError):
            await provider.chat([{"role": "user", "content": "hi"}])

        assert len(fake.calls) == 1

    async def test_chat_does_not_fall_back_on_a_bad_request(
        self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _RecordingAcompletion(
            error=litellm.exceptions.BadRequestError(
                message="invalid request", llm_provider="gemini", model="gemini/gemini-3.5-flash-lite"
            ),
            fail_times=100,
        )
        monkeypatch.setattr("litellm.acompletion", fake)

        with pytest.raises(litellm.exceptions.BadRequestError):
            await provider.chat([{"role": "user", "content": "hi"}])

        assert len(fake.calls) == 1

    async def test_chat_does_not_fall_back_on_context_window_exceeded(
        self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _RecordingAcompletion(
            error=litellm.exceptions.ContextWindowExceededError(
                message="context too long", llm_provider="gemini", model="gemini/gemini-3.5-flash-lite"
            ),
            fail_times=100,
        )
        monkeypatch.setattr("litellm.acompletion", fake)

        with pytest.raises(litellm.exceptions.ContextWindowExceededError):
            await provider.chat([{"role": "user", "content": "hi"}])

        assert len(fake.calls) == 1

    async def test_chat_does_not_fall_back_when_the_primary_model_is_a_different_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient failure on a non-Gemini primary must not silently route data to Gemini."""
        provider = LlmProvider(chat_model="openai/gpt-4o")
        fake = _RecordingAcompletion(fail_times=100)
        monkeypatch.setattr("litellm.acompletion", fake)

        with pytest.raises(litellm.exceptions.ServiceUnavailableError):
            await provider.chat([{"role": "user", "content": "hi"}])

        assert len(fake.calls) == 1


class TestParallelToolCallsParameter:
    """LiteLLM raises UnsupportedParamsError instead of dropping a parameter the provider lacks."""

    async def test_chat_sends_parallel_tool_calls_to_a_provider_that_accepts_it(
        self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _RecordingAcompletion()
        monkeypatch.setattr("litellm.acompletion", fake)

        await provider.chat([{"role": "user", "content": "hi"}], parallel_tool_calls=True)

        assert fake.calls[0]["parallel_tool_calls"] is True

    async def test_chat_omits_parallel_tool_calls_for_a_provider_that_rejects_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ollama rejects the parameter, so it must not be sent -- otherwise every call fails."""
        provider = LlmProvider(chat_model="ollama/qwen2.5:7b")
        fake = _RecordingAcompletion()
        monkeypatch.setattr("litellm.acompletion", fake)

        await provider.chat([{"role": "user", "content": "hi"}], parallel_tool_calls=True)

        assert "parallel_tool_calls" not in fake.calls[0]
        assert fake.calls[0]["model"] == "ollama/qwen2.5:7b"

    async def test_chat_still_sends_the_rest_of_the_call_to_such_a_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the one unsupported parameter is dropped -- tools and messages still go out."""
        provider = LlmProvider(chat_model="ollama/qwen2.5:7b")
        fake = _RecordingAcompletion()
        monkeypatch.setattr("litellm.acompletion", fake)
        tools = [{"type": "function", "function": {"name": "say"}}]

        await provider.chat([{"role": "user", "content": "hi"}], tools=tools, parallel_tool_calls=True)

        assert fake.calls[0]["tools"] == tools
        assert fake.calls[0]["messages"] == [{"role": "user", "content": "hi"}]

    async def test_accepts_parallel_tool_calls_is_false_for_an_unrecognized_model(self) -> None:
        assert _accepts_parallel_tool_calls("not-a-provider/not-a-model") is False

    async def test_accepts_parallel_tool_calls_is_false_when_the_lookup_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed capability lookup must not become an exception of its own."""

        def boom(**kwargs: Any) -> Any:
            raise RuntimeError("lookup exploded")

        monkeypatch.setattr("litellm.get_supported_openai_params", boom)

        assert _accepts_parallel_tool_calls("gemini/gemini-3.5-flash-lite") is False


class TestDescribeImage:
    async def test_describe_image_returns_the_text(
        self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _RecordingAcompletion(content="a red ball")
        monkeypatch.setattr("litellm.acompletion", fake)

        text = await provider.describe_image("YmFzZTY0", "what do you see?")

        assert text == "a red ball"
        assert fake.calls[0]["model"] == "gemini/gemini-3.5-flash-lite"

    async def test_describe_image_without_a_vlm_model_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = LlmProvider(chat_model="gemini/gemini-3.5-flash-lite", vlm_model=None)
        monkeypatch.setattr("litellm.acompletion", _RecordingAcompletion())

        with pytest.raises(RuntimeError):
            await provider.describe_image("YmFzZTY0", "what do you see?")


class TestTranscribe:
    async def test_transcribe_returns_stripped_text(
        self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_atranscription(**kwargs: Any) -> Any:
            class _Resp:
                text = "  hello there  "

            return _Resp()

        monkeypatch.setattr("litellm.atranscription", fake_atranscription)

        text = await provider.transcribe(b"fake-wav-bytes")

        assert text == "hello there"

    async def test_transcribe_without_an_stt_model_raises(self) -> None:
        provider = LlmProvider(chat_model="gemini/gemini-3.5-flash-lite", stt_model=None)

        with pytest.raises(RuntimeError):
            await provider.transcribe(b"fake-wav-bytes")

    async def test_transcribe_with_an_explicitly_empty_text_returns_empty_string(
        self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit empty transcription (silence) is a legitimate result, not malformed."""

        async def fake_atranscription(**kwargs: Any) -> Any:
            class _Resp:
                text = ""

            return _Resp()

        monkeypatch.setattr("litellm.atranscription", fake_atranscription)

        text = await provider.transcribe(b"fake-wav-bytes")

        assert text == ""

    async def test_transcribe_raises_when_the_response_has_no_text_field(
        self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A response missing 'text' entirely is a malformed/unexpected shape, not silence."""

        async def fake_atranscription(**kwargs: Any) -> Any:
            class _Resp:
                pass

            return _Resp()

        monkeypatch.setattr("litellm.atranscription", fake_atranscription)

        with pytest.raises(RuntimeError):
            await provider.transcribe(b"fake-wav-bytes")

    async def test_transcribe_raises_when_a_dict_response_has_no_text_key(
        self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_atranscription(**kwargs: Any) -> Any:
            return {"other_field": "value"}

        monkeypatch.setattr("litellm.atranscription", fake_atranscription)

        with pytest.raises(RuntimeError):
            await provider.transcribe(b"fake-wav-bytes")


class TestClassifySpeech:
    async def test_empty_text_is_noise(self, provider: LlmProvider) -> None:
        verdict = await provider.classify_speech("   ")

        assert verdict.kind == "noise"

    async def test_known_backchannel_is_noise_without_calling_the_guard_model(
        self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _RecordingAcompletion()
        monkeypatch.setattr("litellm.acompletion", fake)

        verdict = await provider.classify_speech("うん")

        assert verdict.kind == "noise"
        assert fake.calls == []

    async def test_too_short_text_is_noise(self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _RecordingAcompletion()
        monkeypatch.setattr("litellm.acompletion", fake)

        verdict = await provider.classify_speech("ab")

        assert verdict.kind == "noise"
        assert fake.calls == []

    async def test_without_a_guard_model_it_passes_through_as_command(self) -> None:
        provider = LlmProvider(chat_model="gemini/gemini-3.5-flash-lite", guard_model=None)

        verdict = await provider.classify_speech("dance for me")

        assert verdict.kind == "command"
        assert verdict.text == "dance for me"

    async def test_guard_model_verdict_is_used(self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_acompletion(**kwargs: Any) -> _FakeModelResponse:
            return _FakeModelResponse(SpeechVerdict(kind="command", text="dance for me").model_dump_json())

        monkeypatch.setattr("litellm.acompletion", fake_acompletion)

        verdict = await provider.classify_speech("please dance for me right now")

        assert verdict.kind == "command"
        assert verdict.text == "dance for me"

    async def test_ambient_verdict_is_used(self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_acompletion(**kwargs: Any) -> _FakeModelResponse:
            return _FakeModelResponse(SpeechVerdict(kind="ambient", text="I'm hungry").model_dump_json())

        monkeypatch.setattr("litellm.acompletion", fake_acompletion)

        verdict = await provider.classify_speech("boy am I hungry today")

        assert verdict.kind == "ambient"
        assert verdict.text == "I'm hungry"

    async def test_malformed_guard_response_raises(
        self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_acompletion(**kwargs: Any) -> _FakeModelResponse:
            return _FakeModelResponse("not json")

        monkeypatch.setattr("litellm.acompletion", fake_acompletion)

        with pytest.raises(RuntimeError):
            await provider.classify_speech("please dance for me right now")

    async def test_early_reject_logs_the_reason_and_text(
        self, provider: LlmProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)

        await provider.classify_speech("うん")

        assert any("early-reject" in record.message and "うん" in record.message for record in caplog.records)

    async def test_guard_verdict_logs_the_kind(
        self, provider: LlmProvider, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)

        async def fake_acompletion(**kwargs: Any) -> _FakeModelResponse:
            return _FakeModelResponse(SpeechVerdict(kind="command", text="dance for me").model_dump_json())

        monkeypatch.setattr("litellm.acompletion", fake_acompletion)

        await provider.classify_speech("please dance for me right now")

        assert any("command" in record.message for record in caplog.records)


class TestFromSettings:
    def test_from_settings_wires_up_the_model_strings(self) -> None:
        from palmimo_companion_agent.pipeline.settings import PipelineSettings

        settings = PipelineSettings.with_env_file(None)
        provider = LlmProvider.from_settings(settings)

        assert provider.chat_model == settings.chat_model
        assert provider.guard_model == settings.guard_model
        assert provider.vlm_model == settings.vlm_model
        assert provider.stt_model == settings.stt_model
