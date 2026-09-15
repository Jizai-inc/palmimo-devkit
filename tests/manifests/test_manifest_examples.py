"""Every example app's `palmimo.toml` must parse and validate against the spec."""

from pathlib import Path

import pytest

from tools.manifest import AppManifest, load_manifest


REPO_ROOT = Path(__file__).resolve().parents[2]

EXAMPLE_MANIFESTS = [
    REPO_ROOT / "examples" / "teleop" / "palmimo.toml",
    REPO_ROOT / "examples" / "agents" / "companion" / "palmimo.toml",
    REPO_ROOT / "examples" / "agents" / "wakeword" / "palmimo.toml",
]


@pytest.mark.parametrize("manifest_path", EXAMPLE_MANIFESTS, ids=lambda p: p.parent.name)
def test_example_manifest_validates(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    assert isinstance(manifest, AppManifest)
    assert manifest.name
    assert manifest.command
