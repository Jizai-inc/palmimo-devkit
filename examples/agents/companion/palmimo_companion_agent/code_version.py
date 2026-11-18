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
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .settings import PROJECT_ROOT


#: Distributions whose installed versions identify the code when git cannot.
_PACKAGES = ("palmimo-sdk", "palmimo-companion-agent")

_GIT_TIMEOUT_S = 2.0
_SHORT_HASH_LEN = 7

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodeVersion:
    """The code a process started from.

    Args:
        commit: Short hash of ``HEAD``; ``None`` outside a git checkout.
        branch: Checked-out branch; ``None`` when detached or unknown.
        dirty: Whether a tracked file differs from ``HEAD``; ``None`` when it
            could not be determined. Untracked files do not count, so an event
            log written inside the checkout does not mark it dirty.
        packages: Installed version of each distribution in :data:`_PACKAGES`,
            ``None`` for one that is not installed.
    """

    commit: str | None
    branch: str | None
    dirty: bool | None
    packages: dict[str, str | None]


def _git_status(directory: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(directory), "status", "--porcelain=v2", "--branch", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        _log.warning("git could not describe %s: %s", directory, exc.stderr.strip())
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("git could not describe %s: %s", directory, exc)
        return None
    return completed.stdout


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def resolve_code_version(directory: Path = PROJECT_ROOT) -> CodeVersion:
    """Describe the code in *directory*'s checkout and the installed packages."""
    status = _git_status(directory)
    headers: dict[str, str] = {}
    changed = False
    for line in (status or "").splitlines():
        if line.startswith("# "):
            key, _, value = line[2:].partition(" ")
            headers[key] = value
        else:
            changed = True
    oid = headers.get("branch.oid", "")
    head = headers.get("branch.head", "")
    return CodeVersion(
        commit=oid[:_SHORT_HASH_LEN] if oid and oid != "(initial)" else None,
        branch=head if head and head != "(detached)" else None,
        dirty=None if status is None else changed,
        packages={name: _package_version(name) for name in _PACKAGES},
    )
