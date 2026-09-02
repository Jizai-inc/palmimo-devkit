"""Settings for the cascaded (STT -> LLM -> TTS) pipeline runtime.

Adds this runtime's own knobs -- the chat/guard/VLM/STT model strings and the
voice backend -- on top of :class:`~palmimo_companion_agent.settings.CompanionSettings`'s
shared base (hardware attach, servo port, hearing knobs, language, log path).
See that module's docstring for the "one character, two runtimes" split this
mirrors.
"""

from __future__ import annotations

from pydantic import Field

from ..settings import PROJECT_ROOT, CompanionSettings


_DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


class PipelineSettings(CompanionSettings):
    """Runtime configuration for the cascaded chat pipeline."""

    #: LiteLLM model string for the tool-calling chat loop.
    chat_model: str = "gemini/gemini-3.5-flash-lite"
    #: LiteLLM model string for the speech-classification guard.
    guard_model: str = "gemini/gemini-3.5-flash-lite"
    #: LiteLLM model string for image-to-text description (VLM).
    vlm_model: str = "gemini/gemini-3.5-flash-lite"
    #: LiteLLM model string for speech-to-text transcription.
    stt_model: str = "openai/gpt-4o-mini-transcribe"

    #: How long the talker must stop before the utterance is treated as
    #: finished, in seconds; ``None`` keeps the segmenter's own default. One of
    #: the two knobs on the delay between hearing and answering -- the other is
    #: an empty ``guard_model``, which skips the guard call entirely.
    silence_seconds: float | None = None

    #: TTS backend for the chat agent: ``"piper"`` (synthesizes locally; needs
    #: the network once, to download its voice) or
    #: ``"openai"`` (the speech API, faster and clearer, needs the network).
    voice_backend: str = "piper"

    #: Voice for the chosen backend; ``None`` uses that backend's default.
    #: For openai an API voice name; for piper a catalogue voice key, not a
    #: path to a model file (see ``voice_dir`` for where its files live).
    voice_name: str | None = None

    #: Speaking rate, 1.0 being the voice's natural rate and higher faster.
    #: Must be strictly positive: piper's engine divides by this value, so
    #: zero raises ZeroDivisionError and a negative rate is meaningless.
    voice_speed: float = Field(default=1.0, gt=0)

    #: Output gain, 1.0 being the voice's own level. Must be non-negative --
    #: negative gain has no physical meaning for a speaker.
    voice_volume: float = Field(default=1.0, ge=0)

    #: Root holding one directory per piper voice model; ``None`` means the
    #: SDK's shared model cache, where a missing voice is downloaded on first
    #: use.
    voice_dir: str | None = None


def load_settings(**field_values: object) -> PipelineSettings:
    """Build pipeline settings off the project's own ``.env`` file (the normal entry point)."""
    return PipelineSettings.with_env_file(_DEFAULT_ENV_FILE, **field_values)
