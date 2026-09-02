"""Tests for :mod:`palmimo_companion_agent.realtime.settings` -- RealtimeSettings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from palmimo_companion_agent.realtime.settings import RealtimeSettings


def test_defaults_match_the_design() -> None:
    settings = RealtimeSettings.with_env_file(None)
    assert settings.model == "gpt-realtime-2.1"
    assert settings.voice == "coral"
    assert settings.pitch == 1.15
    assert settings.reply_chars == 60
    assert settings.frame_seconds == 10.0
    assert settings.session_seconds == 120.0


def test_env_prefix_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPANION_AGENT_MODEL", "gpt-realtime-2.1-mini")
    monkeypatch.setenv("COMPANION_AGENT_VOICE", "sage")

    settings = RealtimeSettings.with_env_file(None)

    assert settings.model == "gpt-realtime-2.1-mini"
    assert settings.voice == "sage"


def test_keyword_overrides_win_over_everything() -> None:
    settings = RealtimeSettings.with_env_file(None, model="custom-model")
    assert settings.model == "custom-model"


def test_a_bogus_voice_fails_fast_at_settings_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad COMPANION_AGENT_VOICE never reaches argparse's own choices= check (that only covers
    the CLI flag) -- the settings layer must reject it before it ever reaches the API."""
    monkeypatch.setenv("COMPANION_AGENT_VOICE", "not-a-real-voice")

    with pytest.raises(ValidationError):
        RealtimeSettings.with_env_file(None)


def test_every_known_voice_is_accepted() -> None:
    from palmimo_companion_agent.realtime.settings import VOICES

    for voice in VOICES:
        assert RealtimeSettings.with_env_file(None, voice=voice).voice == voice
