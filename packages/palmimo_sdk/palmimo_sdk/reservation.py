# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Advisory, process-wide reservations for Palmimo's logical hardware resources.

Reservations coordinate cooperating SDK users; they do not prevent direct
hardware access. Forked children inherit the lock descriptors, so callers must
not fork while holding reservations.
"""

import errno
import json
import math
import os
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkstemp


SYSTEM_LOCK_ROOT = Path("/run/lock")
SYSTEM_LOCK_DIR = SYSTEM_LOCK_ROOT / "palmimo"
_RESOURCES = frozenset({"servo_bus", "camera", "microphone", "speaker", "display"})
_MUTEX = threading.Lock()


class ReservationError(Exception):
    """Base error for reservation contention and setup failures."""


class ResourceBusyError(ReservationError):
    """A logical resource is held by another process.

    Holder fields are None when the metadata cannot identify a live holder.
    """

    def __init__(
        self,
        resource: str,
        holder_pid: int | None = None,
        holder_user: str | None = None,
        holder_app: str | None = None,
    ) -> None:
        self.resource = resource
        self.holder_pid = holder_pid
        self.holder_user = holder_user
        self.holder_app = holder_app
        holder = f"{holder_app} ({holder_user}, pid {holder_pid})" if holder_pid is not None else "an unknown holder"
        super().__init__(f"Resource '{resource}' is busy: used by {holder}.")


class ReservationSetupError(ReservationError):
    """Reservation storage cannot be used; ownership is available when known."""

    def __init__(
        self,
        message: str,
        *,
        owner: str | None = None,
        group: str | None = None,
    ) -> None:
        self.owner = owner
        self.group = group
        super().__init__(message)


@dataclass
class _HeldResource:
    fd: int | None
    references: int = 1


_HELD: dict[Path, _HeldResource] = {}


class Reservation:
    """One reference to an acquired resource; release it when its device closes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._released = False

    def release(self) -> None:
        """Release this reference once, unlocking after the last reference."""
        with _MUTEX:
            if self._released:
                return
            held = _HELD[self._path]
            self._released = True
            held.references -= 1
            if held.references == 0:
                del _HELD[self._path]
                # Never unlink: waiters must continue to lock the same inode.
                if held.fd is not None:
                    os.close(held.fd)


# fcntl, pwd and grp are imported where used: the docs site runs the SDK
# under Pyodide, which lists all three as removed modules.
def _user_name(uid: int) -> str:
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return str(uid)


def _group_name(gid: int) -> str:
    try:
        import grp

        return grp.getgrgid(gid).gr_name
    except (ImportError, KeyError):
        return str(gid)


def _setup_error(path: Path, error: OSError | None = None) -> ReservationSetupError:
    owner: str | None = None
    group: str | None = None
    current = path
    while True:
        try:
            info = current.stat()
            break
        except OSError:
            if current == current.parent:
                info = None
                break
            current = current.parent
    if info is not None:
        owner = _user_name(info.st_uid)
        group = _group_name(info.st_gid)
    ownership = (
        f" Existing path '{current}': owner: {owner}; group: {group}; mode: {info.st_mode & 0o7777:04o}."
        if info is not None
        else " Ownership and permissions unavailable."
    )
    detail = f" {error}" if error is not None else ""
    return ReservationSetupError(
        f"Cannot use reservation path '{path}'.{ownership}{detail}",
        owner=owner,
        group=group,
    )


def _lock_directory() -> Path:
    override = os.environ.get("PALMIMO_LOCK_DIR")
    if override is not None:
        directory = Path(override)
    elif SYSTEM_LOCK_ROOT.exists():
        directory = SYSTEM_LOCK_DIR
    else:
        directory = Path(tempfile.gettempdir()) / f"palmimo-locks-{os.getuid() if hasattr(os, 'getuid') else 'local'}"
    try:
        try:
            directory.mkdir(parents=True)
        except FileExistsError:
            if not directory.is_dir():
                raise
        else:
            directory.chmod(0o1777)
        if not os.access(directory, os.W_OK | os.X_OK):
            raise _setup_error(directory)
    except OSError as error:
        raise _setup_error(directory, error) from error
    return directory.resolve()


def _open_lock(path: Path) -> int:
    try:
        try:
            return os.open(path, os.O_RDWR)
        except FileNotFoundError:
            pass
        fd, temporary = mkstemp(prefix=".palmimo-lock-", dir=path.parent)
        try:
            os.fchmod(fd, 0o666)
            # Publishing only after chmod keeps restrictive umasks invisible to peers.
            with suppress(FileExistsError):
                os.link(temporary, path)
        finally:
            os.close(fd)
            os.unlink(temporary)
        return os.open(path, os.O_RDWR)
    except OSError as error:
        raise _setup_error(path, error) from error


def _busy_error(resource: str, fd: int) -> ResourceBusyError:
    try:
        data = json.loads(os.pread(fd, 65536, 0))
        if not isinstance(data, dict):
            return ResourceBusyError(resource)
        pid, user, app, acquired_at = (data.get(key) for key in ("pid", "user", "app", "acquired_at"))
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(user, str)
            or not isinstance(app, str)
            or not isinstance(acquired_at, str)
        ):
            return ResourceBusyError(resource)
        with suppress(PermissionError):
            os.kill(pid, 0)
        return ResourceBusyError(resource, pid, user, app)
    except (OSError, ValueError, OverflowError):
        return ResourceBusyError(resource)


def _validate(resource: str, timeout: float | None) -> None:
    if resource not in _RESOURCES:
        raise ValueError(f"Unknown Palmimo resource: {resource!r}")
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        raise ValueError("Reservation timeout must be a finite, non-negative number")


def acquire_resource(resource: str, *, timeout: float | None = None) -> Reservation:
    """Acquire a resource reference for a backend's open/close lifecycle.

    Args:
        resource: One of servo_bus, camera, microphone, speaker, display.
        timeout: Maximum wait in seconds; None fails immediately on contention.

    Raises:
        ResourceBusyError: Another process holds the resource past the deadline.
        ReservationSetupError: The reservation directory or file is unusable.
        ValueError: The resource or timeout is invalid.
    """
    _validate(resource, timeout)
    try:
        import fcntl
    except ImportError:
        path = Path(resource)
        with _MUTEX:
            if path in _HELD:
                _HELD[path].references += 1
            else:
                _HELD[path] = _HeldResource(None)
        return Reservation(path)
    deadline = time.monotonic() + (timeout or 0)
    path = _lock_directory() / f"{resource}.lock"
    while True:
        with _MUTEX:
            if path in _HELD:
                _HELD[path].references += 1
                return Reservation(path)
            fd = _open_lock(path)
            acquired = False
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno not in {errno.EAGAIN, errno.EACCES}:
                        raise _setup_error(path, error) from error
                    busy = _busy_error(resource, fd)
                else:
                    metadata = {
                        "pid": os.getpid(),
                        "user": _user_name(os.geteuid()),
                        "app": os.environ.get("PALMIMO_APP_ID", Path(sys.argv[0]).name),
                        "acquired_at": datetime.now(UTC).isoformat(),
                    }
                    try:
                        with os.fdopen(os.dup(fd), "w", encoding="utf-8") as stream:
                            stream.seek(0)
                            json.dump(metadata, stream)
                            stream.truncate()
                    except OSError as error:
                        raise _setup_error(path, error) from error
                    _HELD[path] = _HeldResource(fd)
                    acquired = True
                    return Reservation(path)
            finally:
                if not acquired:
                    os.close(fd)
        remaining = deadline - time.monotonic()
        if timeout is None or remaining <= 0:
            raise busy
        time.sleep(min(0.05, remaining))


@contextmanager
def reserve(*resources: str, timeout: float | None = None) -> Iterator[None]:
    """Reserve resources in lexical order, rolling back on any failure.

    Args:
        *resources: Logical resources to hold for the context's duration.
        timeout: Total contention wait budget in seconds; None fails immediately.
    """
    for resource in resources:
        _validate(resource, timeout)
    if not resources and timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        raise ValueError("Reservation timeout must be a finite, non-negative number")
    deadline = time.monotonic() + (timeout or 0)
    reservations: list[Reservation] = []
    try:
        for resource in sorted(set(resources)):
            remaining = None if timeout is None else max(0.0, deadline - time.monotonic())
            reservations.append(acquire_resource(resource, timeout=remaining))
        yield
    finally:
        for reservation in reversed(reservations):
            reservation.release()
