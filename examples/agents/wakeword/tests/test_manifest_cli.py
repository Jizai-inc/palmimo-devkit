"""`palmimo.toml`'s defaults must reach `palmimo_wakeword_agent.main`'s CLI unchanged.

A default here that drifts from the typer option it names, or from
`WakewordAgentSettings`'s own field default, would make Palmimo Portal launch
this app with a flag it silently rejects or mis-parses -- this is the
contract that catches that before it ships.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from palmimo_wakeword_agent import main as main_module
from palmimo_wakeword_agent.settings import WakewordAgentSettings


MANIFEST_PATH = Path(__file__).resolve().parents[1] / "palmimo.toml"


def _load_manifest() -> dict[str, Any]:
    return tomllib.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


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


def test_manifest_defaults_parse_and_match_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _load_manifest()
    argv = _build_argv(manifest)
    captured: dict[str, object] = {}

    def fake_build_runtime(settings: WakewordAgentSettings) -> None:
        captured["settings"] = settings
        raise SystemExit(0)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    monkeypatch.setattr(main_module, "build_runtime", fake_build_runtime)

    result = CliRunner().invoke(main_module.app, argv)

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert isinstance(settings, WakewordAgentSettings)
    defaults = WakewordAgentSettings.with_env_file(None)
    assert settings.language == manifest["params"]["language"]["default"] == defaults.language
    assert settings.command_model == manifest["params"]["command_model"]["default"] == defaults.command_model
    assert settings.stt_model == manifest["params"]["stt_model"]["default"] == defaults.stt_model
    assert settings.speaker_device == manifest["params"]["speaker_device"]["default"] == defaults.speaker_device
    assert settings.tts is (not manifest["params"]["no_tts"]["default"]) is defaults.tts
