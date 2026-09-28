"""`palmimo.toml`'s defaults must reach `palmimo_teleop.main`'s CLI unchanged.

A default here that drifts from the typer option it names would make Palmimo
Portal launch this app with a flag it silently rejects or mis-parses --
this is the contract that catches that before it ships.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from palmimo_teleop import main as main_module


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


class _DummyRobot:
    has_connectable_resource = False


def test_manifest_defaults_parse_and_match_cli_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _load_manifest()
    argv = _build_argv(manifest)
    captured: dict[str, object] = {}

    def fake_build_robot(*, dry_run: bool, gait_speed: float, fps: int, servo_port: str | None) -> _DummyRobot:
        captured["dry_run"] = dry_run
        captured["gait_speed"] = gait_speed
        captured["fps"] = fps
        return _DummyRobot()

    def fake_build_camera(no_camera: bool) -> None:
        captured["no_camera"] = no_camera
        return None

    def fake_uvicorn_run(app_instance: object, **kwargs: object) -> None:
        captured["port"] = kwargs["port"]
        raise SystemExit(0)

    monkeypatch.setattr(main_module, "_build_robot", fake_build_robot)
    monkeypatch.setattr(main_module, "_build_camera", fake_build_camera)
    monkeypatch.setattr(main_module.uvicorn, "run", fake_uvicorn_run)

    result = CliRunner().invoke(main_module.app, argv)

    assert result.exit_code == 0, result.output
    assert manifest["params"]["port"]["default"] == main_module.DEFAULT_PORT
    assert manifest["params"]["gait_speed"]["default"] == pytest.approx(main_module.DEFAULT_GAIT_SPEED)
    assert captured["port"] == main_module.DEFAULT_PORT
    assert captured["fps"] == manifest["params"]["fps"]["default"]
    assert captured["gait_speed"] == pytest.approx(main_module.DEFAULT_GAIT_SPEED)
    assert captured["dry_run"] is False
    assert captured["no_camera"] is False
