"""Tests for :mod:`palmimo_companion_agent.settings` -- the shared base only.

Every test disables the project ``.env`` file (via ``with_env_file(None)``)
and clears the relevant env vars first, so a developer's real environment or
an actual ``.env`` in the project directory can never leak into the
assertions. Pipeline-only fields (chat/guard/VLM/STT models, voice) are
covered by tests/pipeline/test_settings.py instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from palmimo_companion_agent.settings import CompanionSettings


_ENV_VARS = [
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "COMPANION_AGENT_HARDWARE",
    "COMPANION_AGENT_PORT",
    "COMPANION_AGENT_ECHO_CANCEL",
    "COMPANION_AGENT_NEAR_CHANNEL",
    "COMPANION_AGENT_REFERENCE_CHANNEL",
    "COMPANION_AGENT_SPEAKER_DEVICE",
    "COMPANION_AGENT_LANGUAGE",
    "COMPANION_AGENT_LOG_PATH",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_when_nothing_is_set() -> None:
    settings = CompanionSettings.with_env_file(None)

    assert settings.hardware is True
    assert settings.port is None
    assert settings.echo_cancel is True
    assert settings.near_channel == 0
    assert settings.reference_channel == 5
    assert settings.speaker_device == "ReSpeaker"
    assert settings.language == "ja"
    assert settings.log_path is None


def test_speaker_device_can_be_emptied_to_fall_back_to_the_alsa_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unset env var takes the "ReSpeaker" default, so "no preference" has
    # to be expressible: SpeakerConfig reads a falsy hint as "issue aplay
    # without -D", which is what an empty value here is asking for.
    monkeypatch.setenv("COMPANION_AGENT_SPEAKER_DEVICE", "")

    settings = CompanionSettings.with_env_file(None)

    assert not settings.speaker_device


def test_prefixed_env_vars_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPANION_AGENT_HARDWARE", "false")
    monkeypatch.setenv("COMPANION_AGENT_PORT", "/dev/ttyACM0")
    monkeypatch.setenv("COMPANION_AGENT_ECHO_CANCEL", "false")
    monkeypatch.setenv("COMPANION_AGENT_NEAR_CHANNEL", "2")
    monkeypatch.setenv("COMPANION_AGENT_REFERENCE_CHANNEL", "3")
    monkeypatch.setenv("COMPANION_AGENT_SPEAKER_DEVICE", "UACDemoV10")
    monkeypatch.setenv("COMPANION_AGENT_LANGUAGE", "en")
    monkeypatch.setenv("COMPANION_AGENT_LOG_PATH", "/tmp/companion-events.jsonl")

    settings = CompanionSettings.with_env_file(None)

    assert settings.hardware is False
    assert settings.port == "/dev/ttyACM0"
    assert settings.echo_cancel is False
    assert settings.near_channel == 2
    assert settings.reference_channel == 3
    assert settings.speaker_device == "UACDemoV10"
    assert settings.language == "en"
    assert settings.log_path == Path("/tmp/companion-events.jsonl")


def test_with_env_file_none_does_not_load_process_env_keys_from_disk(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=sk-from-dotenv\n", encoding="utf-8")

    CompanionSettings.with_env_file(None)

    assert "GEMINI_API_KEY" not in __import__("os").environ


def test_with_env_file_loads_prefixed_vars_and_api_keys_into_process_env(tmp_path: Path) -> None:
    import os

    env_file = tmp_path / ".env"
    env_file.write_text(
        "COMPANION_AGENT_PORT=/dev/from-dotenv\nGEMINI_API_KEY=sk-from-dotenv\n",
        encoding="utf-8",
    )

    settings = CompanionSettings.with_env_file(env_file)

    assert settings.port == "/dev/from-dotenv"
    # GEMINI_API_KEY is not a settings field -- LiteLLM reads it straight out
    # of the process environment, so with_env_file() must load it there too.
    assert os.environ.get("GEMINI_API_KEY") == "sk-from-dotenv"
    os.environ.pop("GEMINI_API_KEY", None)


def test_process_env_wins_over_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("COMPANION_AGENT_PORT=/dev/from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("COMPANION_AGENT_PORT", "/dev/from-process-env")

    settings = CompanionSettings.with_env_file(env_file)

    assert settings.port == "/dev/from-process-env"
