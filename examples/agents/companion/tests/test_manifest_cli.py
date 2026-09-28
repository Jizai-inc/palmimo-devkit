"""Both `palmimo.toml` manifests' defaults must reach their CLI unchanged.

A default here that drifts from the flag it names, or from the settings
field it feeds, would make Palmimo Portal launch the app with a value it
silently rejects or mis-parses -- this is the contract that catches that
before it ships.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from palmimo_companion_agent.main import app as pipeline_app
from palmimo_companion_agent.pipeline.settings import PipelineSettings
from palmimo_companion_agent.realtime.app import _build_parser
from palmimo_companion_agent.realtime.settings import RealtimeSettings


PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: `PipelineSettings.with_env_file`/`RealtimeSettings.with_env_file` load API
#: keys straight into `os.environ` (LiteLLM reads them directly, not through
#: a settings field), and some tests write `COMPANION_AGENT_*` vars into the
#: real process environment the same way -- without this cleanup, whichever
#: test ran first in the session leaks its value into these assertions.
_ENV_VARS = [
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "COMPANION_AGENT_LANGUAGE",
    "COMPANION_AGENT_CHAT_MODEL",
    "COMPANION_AGENT_STT_MODEL",
    "COMPANION_AGENT_VOICE_BACKEND",
    "COMPANION_AGENT_VOICE_SPEED",
    "COMPANION_AGENT_VOICE_VOLUME",
    "COMPANION_AGENT_MODEL",
    "COMPANION_AGENT_VOICE",
    "COMPANION_AGENT_PITCH",
    "COMPANION_AGENT_REPLY_CHARS",
    "COMPANION_AGENT_FRAME_SECONDS",
    "COMPANION_AGENT_SESSION_SECONDS",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _load_manifest(name: str) -> dict[str, Any]:
    return tomllib.loads((PROJECT_ROOT / name).read_text(encoding="utf-8"))


def _build_argv(manifest: dict[str, Any]) -> list[str]:
    """Render `command`'s placeholders with each param's own default (doc/reference/app-manifest.md)."""
    params: dict[str, Any] = manifest["params"]
    argv: list[str] = []
    for element in manifest["command"][1:]:
        name = element[1:-1] if element.startswith("{") and element.endswith("}") else None
        if name is not None and params.get(name, {}).get("type") == "bool":
            if params[name]["default"]:
                argv.append(params[name]["flag"])
            continue
        rendered = element
        for param_name, spec in params.items():
            if spec["type"] == "bool":
                continue
            rendered = rendered.replace("{" + param_name + "}", str(spec["default"]))
        argv.append(rendered)
    return argv


def test_pipeline_manifest_defaults_parse_and_match_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _load_manifest("palmimo.toml")
    argv = _build_argv(manifest)
    captured: dict[str, PipelineSettings] = {}

    async def fake_run_cli(settings: PipelineSettings, *, read_stdin: bool = True) -> None:
        captured["settings"] = settings

    monkeypatch.setattr("palmimo_companion_agent.pipeline.ui.cli.run_cli", fake_run_cli)

    result = CliRunner().invoke(pipeline_app, argv)

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    defaults = PipelineSettings.with_env_file(None)
    for name in ("language", "chat_model", "stt_model", "voice_backend"):
        assert getattr(settings, name) == manifest["params"][name]["default"] == getattr(defaults, name)
    for name in ("voice_speed", "voice_volume"):
        assert getattr(settings, name) == pytest.approx(manifest["params"][name]["default"])
        assert getattr(settings, name) == pytest.approx(getattr(defaults, name))


def test_realtime_manifest_defaults_parse_and_match_settings_defaults() -> None:
    manifest = _load_manifest("palmimo.realtime.toml")
    argv = _build_argv(manifest)

    parsed = _build_parser().parse_args(argv)

    defaults = RealtimeSettings.with_env_file(None)
    assert parsed.seconds == pytest.approx(manifest["params"]["seconds"]["default"])
    assert parsed.seconds == pytest.approx(defaults.session_seconds)
    assert parsed.model == manifest["params"]["model"]["default"] == defaults.model
    assert parsed.voice == manifest["params"]["voice"]["default"] == defaults.voice
    assert parsed.pitch == pytest.approx(manifest["params"]["pitch"]["default"])
    assert parsed.pitch == pytest.approx(defaults.pitch)
    assert parsed.reply_chars == manifest["params"]["reply_chars"]["default"] == defaults.reply_chars
    assert parsed.frame_seconds == pytest.approx(manifest["params"]["frame_seconds"]["default"])
    assert parsed.frame_seconds == pytest.approx(defaults.frame_seconds)
