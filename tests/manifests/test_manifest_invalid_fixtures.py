"""Every fixture under tests/fixtures/manifests/invalid/ must fail validation.

Each fixture is a minimal manifest violating exactly one rule from
doc/reference/app-manifest.md's validation table (named in the file's leading
comment, for a human reader -- this test checks the behavior, not that text).
"""

from pathlib import Path

import pytest

from tools.manifest import ManifestError, load_manifest


FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "manifests" / "invalid"
INVALID_MANIFESTS = sorted(FIXTURES_DIR.glob("*.toml"))


def test_invalid_fixtures_directory_is_not_empty() -> None:
    assert INVALID_MANIFESTS, f"no fixtures found under {FIXTURES_DIR}"


@pytest.mark.parametrize("manifest_path", INVALID_MANIFESTS, ids=lambda p: p.stem)
def test_invalid_fixture_fails_validation(manifest_path: Path) -> None:
    with pytest.raises(ManifestError):
        load_manifest(manifest_path)
