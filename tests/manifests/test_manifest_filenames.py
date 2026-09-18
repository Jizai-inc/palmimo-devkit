"""Which filenames in an app directory are `palmimo.toml` manifests (doc/reference/app-manifest.md)."""

from pathlib import Path

import pytest

from tools.manifest import discover_manifests, is_manifest_filename


@pytest.mark.parametrize(
    "name",
    ["palmimo.toml", "palmimo.realtime.toml", "palmimo.a1-b.toml"],
)
def test_is_manifest_filename_accepts_default_and_variants(name: str) -> None:
    assert is_manifest_filename(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "palmimo_backup.toml",
        "palmimo.TOML",
        "palmimo..toml",
        "palmimo.Realtime.toml",
        "xpalmimo.toml",
    ],
)
def test_is_manifest_filename_rejects_non_manifests(name: str) -> None:
    assert is_manifest_filename(name) is False


def test_discover_manifests_finds_default_and_variants_non_recursively(tmp_path: Path) -> None:
    (tmp_path / "palmimo.toml").write_text("")
    (tmp_path / "palmimo.realtime.toml").write_text("")
    (tmp_path / "palmimo_backup.toml").write_text("")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "palmimo.toml").write_text("")

    found = discover_manifests(tmp_path)

    assert found == [tmp_path / "palmimo.realtime.toml", tmp_path / "palmimo.toml"]
