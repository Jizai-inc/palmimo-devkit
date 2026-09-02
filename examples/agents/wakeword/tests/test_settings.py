"""Tests for :mod:`palmimo_wakeword_agent.settings`.

Every test disables the project ``.env`` file (via ``with_env_file(None)``) and clears
the relevant env vars first, so a developer's real environment or an actual
``.env`` in the project directory can never leak into the assertions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from palmimo_wakeword_agent.settings import WakewordAgentSettings


_ENV_VARS = [
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "WAKEWORD_AGENT_COMMAND_MODEL",
    "WAKEWORD_AGENT_STT_MODEL",
    "WAKEWORD_AGENT_LANGUAGE",
    "WAKEWORD_AGENT_DEVICE",
    "WAKEWORD_AGENT_SPEAKER_DEVICE",
    "WAKEWORD_AGENT_TTS",
    "WAKEWORD_AGENT_SERVO",
    "WAKEWORD_AGENT_SERVO_PORT",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_when_nothing_is_set() -> None:
    settings = WakewordAgentSettings.with_env_file(None)

    assert settings.openai_api_key is None
    assert settings.gemini_api_key is None
    assert settings.command_model == "gemini-3.5-flash-lite"
    assert settings.stt_model == "gpt-4o-mini-transcribe"
    assert settings.language == "en"
    assert settings.device is None
    assert settings.speaker_device == "ReSpeaker"
    assert settings.tts is True
    assert settings.servo is True
    assert settings.servo_port is None


def test_prefixed_env_vars_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAKEWORD_AGENT_COMMAND_MODEL", "gpt-4o")
    monkeypatch.setenv("WAKEWORD_AGENT_STT_MODEL", "whisper-1")
    monkeypatch.setenv("WAKEWORD_AGENT_LANGUAGE", "ja")
    monkeypatch.setenv("WAKEWORD_AGENT_DEVICE", "USB Mic")
    monkeypatch.setenv("WAKEWORD_AGENT_SPEAKER_DEVICE", "UACDemoV10")
    monkeypatch.setenv("WAKEWORD_AGENT_TTS", "false")
    monkeypatch.setenv("WAKEWORD_AGENT_SERVO", "false")
    monkeypatch.setenv("WAKEWORD_AGENT_SERVO_PORT", "/dev/ttyACM0")

    settings = WakewordAgentSettings.with_env_file(None)

    assert settings.command_model == "gpt-4o"
    assert settings.stt_model == "whisper-1"
    assert settings.language == "ja"
    assert settings.device == "USB Mic"
    assert settings.speaker_device == "UACDemoV10"
    assert settings.tts is False
    assert settings.servo is False
    assert settings.servo_port == "/dev/ttyACM0"


def test_openai_api_key_loads_without_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")

    settings = WakewordAgentSettings.with_env_file(None)

    assert settings.openai_api_key == "sk-test-123"


def test_gemini_api_key_loads_without_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-123")

    settings = WakewordAgentSettings.with_env_file(None)

    assert settings.gemini_api_key == "gemini-test-123"


def test_env_file_loads_and_process_env_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("WAKEWORD_AGENT_COMMAND_MODEL=from-dotenv\nOPENAI_API_KEY=sk-from-dotenv\n", encoding="utf-8")

    settings = WakewordAgentSettings.with_env_file(env_file)
    assert settings.command_model == "from-dotenv"
    assert settings.openai_api_key == "sk-from-dotenv"

    monkeypatch.setenv("WAKEWORD_AGENT_COMMAND_MODEL", "from-process-env")
    settings_with_process_env = WakewordAgentSettings.with_env_file(env_file)
    assert settings_with_process_env.command_model == "from-process-env"


class TestMerged:
    def test_no_overrides_leaves_settings_unchanged(self) -> None:
        settings = WakewordAgentSettings.with_env_file(None)

        merged = settings.merged()

        assert merged == settings

    def test_values_override_settings(self) -> None:
        settings = WakewordAgentSettings.with_env_file(None)

        merged = settings.merged(device="USB Mic", command_model="gpt-4o", stt_model="whisper-1", language="ja")

        assert merged.device == "USB Mic"
        assert merged.command_model == "gpt-4o"
        assert merged.stt_model == "whisper-1"
        assert merged.language == "ja"

    def test_empty_speaker_device_overrides_the_default_instead_of_being_ignored(self) -> None:
        """``--speaker-device ''`` is the only way to ask for the ALSA default
        over the field's own ReSpeaker default, so an empty string has to count
        as "passed" here -- unlike ``None``, which means "not passed at all"."""
        settings = WakewordAgentSettings.with_env_file(None)

        merged = settings.merged(speaker_device="")

        assert merged.speaker_device == ""

    def test_speaker_device_none_leaves_the_default_unchanged(self) -> None:
        settings = WakewordAgentSettings.with_env_file(None)

        merged = settings.merged(speaker_device=None)

        assert merged.speaker_device == "ReSpeaker"

    def test_language_none_leaves_default_unchanged(self) -> None:
        settings = WakewordAgentSettings.with_env_file(None)

        merged = settings.merged(language=None)

        assert merged.language == "en"

    def test_tts_flips_false(self) -> None:
        settings = WakewordAgentSettings.with_env_file(None)

        merged = settings.merged(tts=False)

        assert merged.tts is False

    def test_tts_none_leaves_default_true(self) -> None:
        settings = WakewordAgentSettings.with_env_file(None)

        merged = settings.merged(tts=None)

        assert merged.tts is True

    def test_servo_flips_false(self) -> None:
        settings = WakewordAgentSettings.with_env_file(None)

        merged = settings.merged(servo=False)

        assert merged.servo is False

    def test_servo_none_leaves_default_true(self) -> None:
        settings = WakewordAgentSettings.with_env_file(None)

        merged = settings.merged(servo=None)

        assert merged.servo is True

    def test_servo_port_overrides_settings(self) -> None:
        settings = WakewordAgentSettings.with_env_file(None)

        merged = settings.merged(servo_port="/dev/ttyACM3")

        assert merged.servo_port == "/dev/ttyACM3"

    def test_servo_port_none_leaves_default_unchanged(self) -> None:
        settings = WakewordAgentSettings.with_env_file(None)

        merged = settings.merged(servo_port=None)

        assert merged.servo_port is None
