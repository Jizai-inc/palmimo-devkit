"""Which code a running process is: resolved once at startup, never at write time.

A checkout can be pulled while the process keeps running the code it started
with, so a version read when a log line is written can name code that is not
running. :func:`resolve_code_version` is called once, when the process starts,
and the result is carried to wherever it is logged.

Every field degrades to ``None`` instead of raising: an installed copy has no
git checkout, a device may have no ``git`` binary, and neither is a reason to
stop logging.
"""

from __future__ import annotations

import importlib.metadata
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .settings import PROJECT_ROOT


#: Distributions whose installed versions identify the code when git cannot.
_PACKAGES = ("palmimo-sdk", "palmimo-companion-agent")

_GIT_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class CodeVersion:
    """The code a process started from.

    Args:
        commit: Short hash of ``HEAD``; ``None`` outside a git checkout.
        branch: Checked-out branch; ``None`` when detached or unknown.
        dirty: Whether the working tree differs from ``HEAD``, untracked files
            included; ``None`` when it could not be determined.
        packages: Installed version of each distribution in :data:`_PACKAGES`,
            ``None`` for one that is not installed.
    """

    commit: str | None
    branch: str | None
    dirty: bool | None
    packages: dict[str, str | None]


def _git(directory: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip()


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def resolve_code_version(directory: Path = PROJECT_ROOT) -> CodeVersion:
    """Describe the code in *directory*'s checkout and the installed packages."""
    commit = _git(directory, "rev-parse", "--short", "HEAD")
    branch = _git(directory, "rev-parse", "--abbrev-ref", "HEAD") if commit else None
    status = _git(directory, "status", "--porcelain") if commit else None
    return CodeVersion(
        commit=commit or None,
        branch=None if branch in (None, "", "HEAD") else branch,
        dirty=None if status is None else bool(status),
        packages={name: _package_version(name) for name in _PACKAGES},
    )
