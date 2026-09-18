"""Every standalone example's lock must resolve to wheels on the robot.

The robot never builds a package from source on-device (no compiler toolchain
in the shipped image, and doing it at first boot would be unacceptably slow).
A lock that resolves a source-only distribution for the robot's Python/
platform combination installs fine on a contributor's machine and then fails
on-device: `uv pip install` falls back to a source build there instead of
refusing, so the failure is silent until someone actually deploys.
"""

import subprocess
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet

from tests.contracts.test_self_containment import EXAMPLE_PROJECT_DIRS, _load


# CPython minors the shipped image can run: 3.13 ships on the image, and the
# platform (uv) can download 3.12 on demand. Ordered newest first so the
# device-python lookup below picks the newest one an example's
# `requires-python` allows -- the same minor uv itself would pick when
# installing the example unpinned.
DEVICE_PYTHONS = ["3.13", "3.12"]

# The robot's fixed target platform for every example, independent of the
# machine running this test.
DEVICE_PLATFORM = "aarch64-unknown-linux-gnu"


def _device_python(project_dir: Path) -> str:
    requires_python = _load(project_dir / "pyproject.toml")["project"]["requires-python"]
    specifier = SpecifierSet(requires_python)
    for candidate in DEVICE_PYTHONS:
        if specifier.contains(candidate):
            return candidate
    raise ValueError(
        f"{project_dir}: requires-python {requires_python!r} matches none of "
        f"DEVICE_PYTHONS {DEVICE_PYTHONS} -- add the robot's Python to the list "
        "or this example cannot ship on-device"
    )


@pytest.mark.parametrize("project_dir", EXAMPLE_PROJECT_DIRS, ids=lambda p: p.name)
def test_example_lock_resolves_to_wheels_on_device(project_dir: Path, tmp_path: Path) -> None:
    python_version = _device_python(project_dir)
    requirements = tmp_path / "requirements.txt"

    subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "-o",
            str(requirements),
        ],
        cwd=project_dir,
        check=True,
        capture_output=True,
        text=True,
    )

    result = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--dry-run",
            "--no-build",
            "--python-platform",
            DEVICE_PLATFORM,
            "--python-version",
            python_version,
            "-r",
            str(requirements),
        ],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"{project_dir.name}'s lock does not resolve to wheels for Python "
        f"{python_version} on {DEVICE_PLATFORM} -- the robot never builds from "
        f"source, so this install would fail on-device:\n{result.stderr}"
    )
