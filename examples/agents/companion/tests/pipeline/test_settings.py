"""Tests for :mod:`palmimo_companion_agent.pipeline.settings`.

Every test disables the project ``.env`` file (via ``with_env_file(None)``)
and clears the relevant env vars first, so a developer's real environment or
an actual ``.env`` in the project directory can never leak into the
assertions. Shared-base fields (hardware, port, hearing knobs, language,
log path) are covered by tests/test_settings.py instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from palmimo_companion_agent.pipeline.settings import PipelineSettings, load_settings


_ENV_VARS = [
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "COMPANION_AGENT_HARDWARE",
    "COMPANION_AGENT_CHAT_MODEL",
    "COMPANION_AGENT_GUARD_MODEL",
    "COMPANION_AGENT_VLM_MODEL",
    "COMPANION_AGENT_STT_MODEL",
    "COMPANION_AGENT_SILENCE_SECONDS",
    "COMPANION_AGENT_VOICE_BACKEND",
    "COMPANION_AGENT_VOICE_NAME",
    "COMPANION_AGENT_VOICE_SPEED",
    "COMPANION_AGENT_VOICE_VOLUME",
    "COMPANION_AGENT_VOICE_DIR",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_when_nothing_is_set() -> None:
    settings = PipelineSettings.with_env_file(None)

    # Inherited from the shared base -- PipelineSettings composes rather than
    # redeclaring these.
    assert settings.hardware is True

    assert settings.chat_model == "gemini/gemini-3.5-flash-lite"
    assert settings.guard_model == "gemini/gemini-3.5-flash-lite"
    assert settings.vlm_model == "gemini/gemini-3.5-flash-lite"
    assert settings.stt_model == "openai/gpt-4o-mini-transcribe"
    assert settings.silence_seconds is None
    assert settings.voice_backend == "piper"
    assert settings.voice_name is None
    assert settings.voice_speed == 1.0
    assert settings.voice_volume == 1.0
    assert settings.voice_dir is None


def test_prefixed_env_vars_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPANION_AGENT_CHAT_MODEL", "openai/gpt-4o")
    monkeypatch.setenv("COMPANION_AGENT_GUARD_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv("COMPANION_AGENT_VLM_MODEL", "openai/gpt-4o")
    monkeypatch.setenv("COMPANION_AGENT_STT_MODEL", "openai/whisper-1")
    monkeypatch.setenv("COMPANION_AGENT_SILENCE_SECONDS", "0.75")
    monkeypatch.setenv("COMPANION_AGENT_VOICE_BACKEND", "openai")
    monkeypatch.setenv("COMPANION_AGENT_VOICE_NAME", "alloy")
    monkeypatch.setenv("COMPANION_AGENT_VOICE_SPEED", "1.5")
    monkeypatch.setenv("COMPANION_AGENT_VOICE_VOLUME", "0.5")
    monkeypatch.setenv("COMPANION_AGENT_VOICE_DIR", "/opt/piper-voices")

    settings = PipelineSettings.with_env_file(None)

    assert settings.chat_model == "openai/gpt-4o"
    assert settings.guard_model == "openai/gpt-4o-mini"
    assert settings.vlm_model == "openai/gpt-4o"
    assert settings.stt_model == "openai/whisper-1"
    assert settings.silence_seconds == 0.75
    assert settings.voice_backend == "openai"
    assert settings.voice_name == "alloy"
    assert settings.voice_speed == 1.5
    assert settings.voice_volume == 0.5
    assert settings.voice_dir == "/opt/piper-voices"


def test_with_env_file_loads_prefixed_vars_and_api_keys_into_process_env(tmp_path: Path) -> None:
    import os

    env_file = tmp_path / ".env"
    env_file.write_text(
        "COMPANION_AGENT_CHAT_MODEL=from-dotenv\nGEMINI_API_KEY=sk-from-dotenv\n",
        encoding="utf-8",
    )

    settings = PipelineSettings.with_env_file(env_file)

    assert settings.chat_model == "from-dotenv"
    # GEMINI_API_KEY is not a settings field -- LiteLLM reads it straight out
    # of the process environment, so with_env_file() must load it there too.
    assert os.environ.get("GEMINI_API_KEY") == "sk-from-dotenv"
    os.environ.pop("GEMINI_API_KEY", None)


def test_load_settings_uses_project_env_file() -> None:
    # The project .env is git-ignored and typically absent in CI/dev checkouts,
    # so this only exercises that load_settings() builds successfully off the
    # package-relative default path (falling back to field defaults).
    settings = load_settings()

    assert settings.chat_model


def test_voice_speed_must_be_strictly_positive() -> None:
    with pytest.raises(ValidationError):
        PipelineSettings.with_env_file(None, voice_speed=0)


def test_voice_speed_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        PipelineSettings.with_env_file(None, voice_speed=-1.0)


def test_voice_volume_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        PipelineSettings.with_env_file(None, voice_volume=-0.1)


def test_voice_volume_allows_zero() -> None:
    settings = PipelineSettings.with_env_file(None, voice_volume=0)

    assert settings.voice_volume == 0
