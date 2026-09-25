"""Behavior contract for the release tag classification release.yml relies on.

`.github/scripts/classify_release_tag.sh` decides two things a tag push
drives: whether the tag is accepted at all, and whether it is a
pre-release (skips the on-main check, gets `gh release create --prerelease`)
-- see doc/guides/releasing.md. This tests that script's behavior directly
rather than the workflow YAML's wording.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CLASSIFY_SCRIPT = REPO_ROOT / ".github" / "scripts" / "classify_release_tag.sh"


def _classify(tag: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(CLASSIFY_SCRIPT), tag],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("v1.2.3", "release"),
        ("examples-v1.2.3", "release"),
        ("v1.2.3-rc1", "prerelease"),
        ("examples-v1.2.3-rc10", "prerelease"),
    ],
)
def test_accepted_tag_is_classified(tag: str, expected: str) -> None:
    result = _classify(tag)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",  # missing v prefix
        "v1.2",  # not X.Y.Z
        "v1.2.3-beta1",  # hyphen suffix other than -rcN
        "examples-1.2.3",  # missing the examples-v prefix's "v"
    ],
)
def test_rejected_tag_fails_with_nonzero_exit(tag: str) -> None:
    result = _classify(tag)
    assert result.returncode != 0
    assert result.stdout == ""
