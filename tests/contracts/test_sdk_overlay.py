"""Local SDK checks respect the compatibility range declared by each example."""

import json
import sys
from pathlib import Path

import pytest

from tools.check_sdk_overlay import allows_sdk_overlay, main


@pytest.mark.parametrize(
    ("dependencies", "sdk_version", "expected"),
    [
        (["palmimo-sdk[agent]>=0.2,<0.3"], "0.2.0", True),
        (["palmimo-sdk[agent]>=0.1.3,<0.2"], "0.2.0", False),
        (["palmimo-sdk"], "0.2.0", True),
        (["palmimo-sdk>=0.2,<0.3"], "0.2.1rc1", True),
        (["palmimo-sdk>=0.2,<0.3"], "0.3.0rc1", False),
        (["palmimo-sdk"], "0.3.0rc1", True),
        (["palmimo_sdk[face]~=0.2.0"], "0.2.0", True),
        (["packaging>=24"], "0.2.0", False),
    ],
)
def test_sdk_overlay_respects_declared_compatibility(dependencies: list[str], sdk_version: str, expected: bool) -> None:
    assert allows_sdk_overlay(dependencies, sdk_version) is expected


@pytest.mark.parametrize("dependencies", [["palmimo-sdk<0.2"], []])
def test_overlay_cli_warns_when_workspace_checks_are_skipped(
    dependencies: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    example = tmp_path / "example.toml"
    sdk = tmp_path / "sdk.toml"
    example.write_text("[project]\ndependencies = " + json.dumps(dependencies))
    sdk.write_text('[project]\nversion = "0.2.0"')
    monkeypatch.setattr(sys, "argv", ["check_sdk_overlay.py", str(example), str(sdk)])

    main()

    output = capsys.readouterr()
    assert output.out == "enabled=false\n"
    assert "::warning::" in output.err
