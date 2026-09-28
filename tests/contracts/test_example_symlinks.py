"""Tracked app examples must not contain symlinks Portal would reject."""

import subprocess
from pathlib import Path


SOFTWARE_ROOT = Path(__file__).resolve().parents[2]


def test_tracked_examples_contain_no_symlinks() -> None:
    result = subprocess.run(
        ["git", "ls-files", "-s", "--", "examples"],
        cwd=SOFTWARE_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    symlinks = [line for line in result.stdout.splitlines() if line.startswith("120000 ")]
    assert symlinks == []
