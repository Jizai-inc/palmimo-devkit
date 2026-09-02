"""Settings for the wake-word agent, backed by pydantic-settings.

Deliberately simple: one ``BaseSettings`` class, no YAML, no custom sources.
All values come from the project ``.env`` file (next to ``.env.sample``) or
the process environment.

Precedence (highest wins):
  1. CLI argument (applied on top in :mod:`palmimo_wakeword_agent.main` via
     :meth:`WakewordAgentSettings.merged`)
  2. process environment (``WAKEWORD_AGENT_*``, or plain ``OPENAI_API_KEY`` /
     ``GEMINI_API_KEY``)
  3. the project ``.env`` file
  4. field default

STT always runs against OpenAI's Whisper API, so ``OPENAI_API_KEY`` is always
required. The command model defaults to a Gemini model reached through
Google's OpenAI-compatible endpoint, which needs ``GEMINI_API_KEY``; pointing
``command_model`` back at a plain OpenAI model (e.g. ``gpt-5-nano``) reverts
to the single OpenAI key. See :mod:`palmimo_wakeword_agent.wiring` for the
client-selection logic and :mod:`palmimo_wakeword_agent.main` for the
preflight checks.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


#: The wakeword example's project directory (parent of this package), where
#: ``.env`` / ``.env.sample`` live.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WakewordAgentSettings(BaseSettings):
    """Runtime configuration for the wake-word agent CLI.

    The wake word itself is not configurable here: it is fixed by the SDK
    (:data:`palmimo_sdk.PALMIMO_NAMES`), because the matcher's sound folding
    and threshold were measured against recordings of that one name -- see
    :mod:`palmimo_sdk.name_match`.
    """

    model_config = SettingsConfigDict(
        env_prefix="WAKEWORD_AGENT_",
        env_file=_PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Not prefixed: shared with other tools/SDKs that read this var directly
    # (e.g. the `openai` SDK falls back to it itself when no key is passed).
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")

    # Not prefixed either, for the same reason: shared with any tooling that
    # reads it directly. Only required when `command_model` points at a
    # Gemini model (see `wiring._chat_client_for` and `main`'s preflight).
    gemini_api_key: str | None = Field(default=None, validation_alias="GEMINI_API_KEY")

    command_model: str = "gemini-3.5-flash-lite"
    #: Whisper-family transcription model, used for BOTH wake-word detection
    #: and command text -- every VAD-segmented utterance goes through it.
    stt_model: str = "gpt-4o-mini-transcribe"

    #: ISO 639-1 language code driving the Whisper transcription hint, the
    #: piper TTS voice (``SpeakerConfig(lang=...)``), and the reply-language
    #: instruction given to the LLM. The SDK speaker currently ships voices
    #: for "en" and "ja" only (see ``palmimo_sdk.io.speaker.SpeakerConfig``).
    language: str = "en"

    #: MicStream device_name_hint (substring match); None uses the system default.
    device: str | None = None

    #: SpeakerConfig device_name_hint (substring match), naming the playback
    #: card by id rather than by an index a replug can change. Defaults to the
    #: array this example already assumes for echo cancellation. Empty or
    #: unmatched falls back to ALSA's default, which is what a machine without
    #: the array does anyway.
    speaker_device: str | None = "ReSpeaker"

    tts: bool = True

    #: Attach a real DynamixelDriver so commands move an actual robot. When
    #: the servo bus can't be probed (no hardware, port not found, etc.) the
    #: agent degrades to compute-only rather than failing startup -- see
    #: ``_build_servo_driver`` in :mod:`palmimo_wakeword_agent.wiring`.
    servo: bool = True

    #: Serial port of the servo bus's USB-to-servo bridge (e.g.
    #: ``/dev/ttyACM0``). ``None`` (default) auto-detects it.
    servo_port: str | None = None

    @classmethod
    def with_env_file(cls, env_file: Path | str | None, **field_values: object) -> WakewordAgentSettings:
        """Build settings reading *env_file* instead of the project ``.env``.

        ``None`` disables dotenv loading entirely (process env still applies)
        — tests use this so a developer's real ``.env`` can't leak in. The
        suppressions below exist because both checkers derive ``__init__``
        from the model fields via ``dataclass_transform`` and don't see
        pydantic-settings' runtime-only ``_env_file`` keyword; funneling
        every such call through this one classmethod keeps the suppression
        in a single place.
        """
        return cls(_env_file=env_file, **field_values)  # type: ignore[call-arg, arg-type]  # pyright: ignore[reportCallIssue]

    def merged(
        self,
        *,
        device: str | None = None,
        speaker_device: str | None = None,
        command_model: str | None = None,
        stt_model: str | None = None,
        language: str | None = None,
        tts: bool | None = None,
        servo: bool | None = None,
        servo_port: str | None = None,
    ) -> WakewordAgentSettings:
        """Layer CLI overrides on top of these settings (CLI > process env > ``.env`` > default).

        Every keyword defaults to ``None``, meaning "not passed on the CLI --
        leave the corresponding setting untouched".
        """
        update: dict[str, object] = {}
        if device is not None:
            update["device"] = device
        # An empty string is a real value here, not "not passed": it is how
        # --speaker-device asks for the ALSA default over the field's own
        # ReSpeaker default.
        if speaker_device is not None:
            update["speaker_device"] = speaker_device
        if command_model is not None:
            update["command_model"] = command_model
        if stt_model is not None:
            update["stt_model"] = stt_model
        if language is not None:
            update["language"] = language
        if tts is not None:
            update["tts"] = tts
        if servo is not None:
            update["servo"] = servo
        if servo_port is not None:
            update["servo_port"] = servo_port
        return self.model_copy(update=update)
