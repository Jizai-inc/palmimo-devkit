"""Tests for :mod:`palmimo_companion_agent.code_version`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from palmimo_companion_agent.code_version import resolve_code_version


def _git(directory: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(directory), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "--initial-branch=trunk")
    (tmp_path / "tracked.txt").write_text("a\n")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "initial")
    return tmp_path


def test_resolve_code_version_reports_commit_branch_and_a_clean_tree(checkout: Path) -> None:
    version = resolve_code_version(checkout)

    assert version.commit
    assert version.branch == "trunk"
    assert version.dirty is False


def test_resolve_code_version_reports_a_dirty_tree_when_a_tracked_file_changed(checkout: Path) -> None:
    (checkout / "tracked.txt").write_text("b\n")

    assert resolve_code_version(checkout).dirty is True


def test_resolve_code_version_keeps_a_tree_clean_when_only_an_untracked_file_appears(checkout: Path) -> None:
    (checkout / "events.jsonl").write_text("{}\n")

    assert resolve_code_version(checkout).dirty is False


def test_resolve_code_version_leaves_git_fields_unknown_outside_a_checkout(tmp_path: Path) -> None:
    version = resolve_code_version(tmp_path)

    assert (version.commit, version.branch, version.dirty) == (None, None, None)
    assert version.packages["palmimo-companion-agent"] is not None


def test_resolve_code_version_leaves_git_fields_unknown_when_git_is_missing(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")

    version = resolve_code_version(checkout)

    assert (version.commit, version.branch, version.dirty) == (None, None, None)
