"""Process-level resource reservation contracts, without hardware."""

import errno
import json
import os
import pwd
import selectors
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from palmimo_sdk import ReservationSetupError, ResourceBusyError
from palmimo_sdk import reservation as rsv


@pytest.fixture
def lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PALMIMO_LOCK_DIR", str(tmp_path / "locks"))
    return tmp_path / "locks"


@contextmanager
def holder(resource: str, directory: Path, metadata: str | None = None) -> Iterator[subprocess.Popen[str]]:
    script = """
import json, os, sys
from pathlib import Path
from typing import Any
from palmimo_sdk.reservation import reserve
with reserve(sys.argv[1]):
    if sys.argv[2] != 'default':
        Path(os.environ['PALMIMO_LOCK_DIR'], sys.argv[1] + '.lock').write_text(sys.argv[2])
    print('ready', flush=True)
    sys.stdin.read()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, resource, metadata if metadata is not None else "default"],
        env={**os.environ, "PALMIMO_LOCK_DIR": str(directory), "PALMIMO_APP_ID": "companion"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "Holder did not become ready"
        assert process.stdout.readline().strip() == "ready"
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def contender(resource: str, directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from palmimo_sdk.reservation import reserve, ResourceBusyError
try:
    with reserve(sys.argv[1]):
        pass
except ResourceBusyError:
    sys.exit(23)
""",
            resource,
        ],
        env={**os.environ, "PALMIMO_LOCK_DIR": str(directory)},
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_reservation_reports_live_holder(lock_dir: Path) -> None:
    with holder("camera", lock_dir) as process, pytest.raises(ResourceBusyError) as caught, rsv.reserve("camera"):
        pytest.fail("Busy resource was acquired")
    assert caught.value.resource == "camera"
    assert caught.value.holder_pid == process.pid
    assert caught.value.holder_user == pwd.getpwuid(os.geteuid()).pw_name
    assert caught.value.holder_app == "companion"


def test_reservation_reacquires_after_sigkill(lock_dir: Path) -> None:
    with holder("camera", lock_dir) as process:
        process.kill()
        process.wait(timeout=10)
        with rsv.reserve("camera"):
            assert contender("camera", lock_dir).returncode == 23


def test_reservation_counts_references_until_last_release(lock_dir: Path) -> None:
    first = rsv.acquire_resource("camera")
    second = rsv.acquire_resource("camera")
    try:
        first.release()
        first.release()
        assert contender("camera", lock_dir).returncode == 23
    finally:
        first.release()
        second.release()
    assert contender("camera", lock_dir).returncode == 0


def test_reservation_shares_resource_between_threads(lock_dir: Path) -> None:
    barrier = threading.Barrier(8)

    def hold() -> None:
        with rsv.reserve("camera"):
            barrier.wait(timeout=10)
            assert contender("camera", lock_dir).returncode == 23

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: hold(), range(8)))
    assert contender("camera", lock_dir).returncode == 0


def test_reservation_creates_world_writable_file_despite_umask(lock_dir: Path) -> None:
    previous = os.umask(0o077)
    try:
        with rsv.reserve("servo_bus"):
            assert stat.S_IMODE((lock_dir / "servo_bus.lock").stat().st_mode) == 0o666
    finally:
        os.umask(previous)


@pytest.mark.parametrize("app", ["portal-app", None])
def test_reservation_writes_holder_metadata(lock_dir: Path, monkeypatch: pytest.MonkeyPatch, app: str | None) -> None:
    if app is None:
        monkeypatch.delenv("PALMIMO_APP_ID", raising=False)
    else:
        monkeypatch.setenv("PALMIMO_APP_ID", app)
    with rsv.reserve("speaker"):
        metadata = json.loads((lock_dir / "speaker.lock").read_text())
    assert metadata["pid"] == os.getpid()
    assert metadata["user"] == pwd.getpwuid(os.geteuid()).pw_name
    assert metadata["app"] == (app if app is not None else Path(sys.argv[0]).name)
    assert metadata["acquired_at"]


@pytest.mark.parametrize(
    "metadata",
    [
        "",
        "broken",
        "[]",
        "{}",
        '{"pid": -1, "user": "alice", "app": "old", "acquired_at": "then"}',
        '{"pid": 2147483647, "user": "alice", "app": "old", "acquired_at": "then"}',
    ],
)
def test_reservation_ignores_invalid_holder(lock_dir: Path, metadata: str) -> None:
    with holder("display", lock_dir, metadata), pytest.raises(ResourceBusyError) as caught, rsv.reserve("display"):
        pass
    assert (caught.value.holder_pid, caught.value.holder_user, caught.value.holder_app) == (None, None, None)


def test_reservation_ignores_unreadable_holder(lock_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with holder("display", lock_dir):

        def unreadable(*args: object) -> bytes:
            raise PermissionError(errno.EACCES, "Cannot read holder")

        monkeypatch.setattr(rsv.os, "pread", unreadable)
        with pytest.raises(ResourceBusyError) as caught, rsv.reserve("display"):
            pass
    assert caught.value.holder_pid is None
    assert caught.value.holder_user is None
    assert caught.value.holder_app is None


@pytest.mark.parametrize("choice", ["override", "system", "new_system", "development"])
def test_reservation_selects_lock_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, choice: str) -> None:
    runtime = tmp_path / "run" / "lock"
    system = runtime / "palmimo"
    override = tmp_path / "override"
    monkeypatch.setattr(rsv, "SYSTEM_LOCK_ROOT", runtime)
    monkeypatch.setattr(rsv, "SYSTEM_LOCK_DIR", system)
    monkeypatch.setattr(rsv.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("PALMIMO_LOCK_DIR", raising=False)
    if choice in {"override", "system"}:
        system.mkdir(parents=True)
    if choice == "new_system":
        runtime.mkdir(parents=True)
    if choice == "override":
        monkeypatch.setenv("PALMIMO_LOCK_DIR", str(override))
    expected = {
        "override": override,
        "system": system,
        "new_system": system,
        "development": tmp_path / f"palmimo-locks-{os.getuid()}",
    }[choice]
    with rsv.reserve("camera"):
        assert contender("camera", expected).returncode == 23


@pytest.mark.parametrize("choice", ["override", "system"])
def test_reservation_creates_shared_directory_despite_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, choice: str
) -> None:
    root = tmp_path / "lock"
    root.mkdir()
    directory = root / "palmimo"
    monkeypatch.setattr(rsv, "SYSTEM_LOCK_ROOT", root)
    monkeypatch.setattr(rsv, "SYSTEM_LOCK_DIR", directory)
    monkeypatch.delenv("PALMIMO_LOCK_DIR", raising=False)
    if choice == "override":
        monkeypatch.setenv("PALMIMO_LOCK_DIR", str(directory))
    previous = os.umask(0o077)
    try:
        with rsv.reserve("camera"):
            assert stat.S_IMODE(directory.stat().st_mode) == 0o1777
    finally:
        os.umask(previous)


@pytest.mark.parametrize("choice", ["override", "system"])
def test_reservation_does_not_fallback_when_directory_creation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, choice: str
) -> None:
    root = tmp_path / "lock"
    root.mkdir()
    directory = root / "palmimo"
    directory.touch()
    monkeypatch.setattr(rsv, "SYSTEM_LOCK_ROOT", root)
    monkeypatch.setattr(rsv, "SYSTEM_LOCK_DIR", directory)
    monkeypatch.setattr(rsv.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("PALMIMO_LOCK_DIR", raising=False)
    if choice == "override":
        monkeypatch.setenv("PALMIMO_LOCK_DIR", str(directory))
    with pytest.raises(ReservationSetupError), rsv.reserve("camera"):
        pass
    assert not list(tmp_path.rglob("*.lock"))


@pytest.mark.skipif(os.geteuid() == 0, reason="Root bypasses directory write permissions")
@pytest.mark.parametrize("choice", ["override", "system"])
def test_reservation_does_not_fallback_when_directory_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, choice: str
) -> None:
    directory = tmp_path / "palmimo" / "locks"
    directory.mkdir(parents=True)
    monkeypatch.setattr(rsv, "SYSTEM_LOCK_ROOT", directory.parent)
    monkeypatch.setattr(rsv, "SYSTEM_LOCK_DIR", directory)
    monkeypatch.delenv("PALMIMO_LOCK_DIR", raising=False)
    if choice == "override":
        monkeypatch.setenv("PALMIMO_LOCK_DIR", str(directory))
    directory.chmod(0o550)
    try:
        with pytest.raises(ReservationSetupError) as caught, rsv.reserve("camera"):
            pass
        assert caught.value.owner == pwd.getpwuid(directory.stat().st_uid).pw_name
    finally:
        directory.chmod(0o770)
    assert not (directory / "camera.lock").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="Root bypasses file write permissions")
def test_reservation_reports_file_permission_owner(lock_dir: Path) -> None:
    lock_dir.mkdir()
    path = lock_dir / "camera.lock"
    path.touch(mode=0o440)
    try:
        with pytest.raises(ReservationSetupError) as caught, rsv.reserve("camera"):
            pass
        assert caught.value.owner == pwd.getpwuid(path.stat().st_uid).pw_name
        import grp

        assert caught.value.group == grp.getgrgid(path.stat().st_gid).gr_name
    finally:
        path.chmod(0o666)


@pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM])
def test_reservation_classifies_open_permission_error(
    lock_dir: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def denied(*args: object, **kwargs: object) -> int:
        raise OSError(code, "Permission denied")

    monkeypatch.setattr(rsv.os, "open", denied)
    with pytest.raises(ReservationSetupError), rsv.reserve("camera"):
        pass


def test_reservation_rolls_back_sorted_multi_resource_acquisition(lock_dir: Path) -> None:
    with holder("display", lock_dir), pytest.raises(ResourceBusyError) as caught, rsv.reserve("display", "camera"):
        pass
    assert caught.value.resource == "display"
    assert contender("camera", lock_dir).returncode == 0


def test_reservation_releases_when_context_body_raises(lock_dir: Path) -> None:
    with pytest.raises(ValueError), rsv.reserve("camera", "speaker"):
        raise ValueError("Application failed")
    assert contender("camera", lock_dir).returncode == 0
    assert contender("speaker", lock_dir).returncode == 0


@pytest.mark.parametrize("resource", ["servo_bus", "camera", "microphone", "speaker", "display"])
def test_reservation_accepts_logical_resource(lock_dir: Path, resource: str) -> None:
    with rsv.reserve(resource):
        assert contender(resource, lock_dir).returncode == 23


@pytest.mark.parametrize("resource", ["invalid", "../camera", "", "/dev/ttyACM0"])
def test_reservation_rejects_unknown_resource_before_acquisition(lock_dir: Path, resource: str) -> None:
    with pytest.raises(ValueError), rsv.reserve("camera", resource):
        pass
    assert not lock_dir.exists()


def test_reservation_waits_only_with_timeout(lock_dir: Path) -> None:
    with holder("camera", lock_dir) as process:
        timer = threading.Timer(0.15, process.kill)
        timer.start()
        try:
            with rsv.reserve("camera", timeout=2):
                assert contender("camera", lock_dir).returncode == 23
        finally:
            timer.join(timeout=10)


def test_reservation_timeout_reports_holder_and_releases_partial_acquisition(lock_dir: Path) -> None:
    with holder("display", lock_dir) as process:
        started = time.monotonic()
        with pytest.raises(ResourceBusyError) as caught, rsv.reserve("display", "camera", timeout=0.15):
            pass
        elapsed = time.monotonic() - started
        assert 0.12 <= elapsed < 2
        assert caught.value.holder_pid == process.pid
        assert contender("camera", lock_dir).returncode == 0


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan")])
def test_reservation_rejects_invalid_timeout(lock_dir: Path, timeout: float) -> None:
    with pytest.raises(ValueError), rsv.reserve("camera", timeout=timeout):
        pass
    assert not lock_dir.exists()


def test_reservation_errors_escape_runtime_error_handlers() -> None:
    assert issubclass(ResourceBusyError, rsv.ReservationError)
    assert issubclass(ReservationSetupError, rsv.ReservationError)
    assert not issubclass(rsv.ReservationError, RuntimeError)


def test_reservation_acquires_multiple_resources_in_lexical_order(lock_dir: Path) -> None:
    def wait_for_resources() -> None:
        with pytest.raises(ResourceBusyError), rsv.reserve("display", "camera", timeout=2):
            pass

    with holder("display", lock_dir), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(wait_for_resources)
        deadline = time.monotonic() + 1
        while contender("camera", lock_dir).returncode != 23:
            assert time.monotonic() < deadline, "Camera was not acquired before display"
            time.sleep(0.01)
        future.result(timeout=5)
    assert contender("camera", lock_dir).returncode == 0


def test_reservation_releases_after_metadata_write_failure(lock_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "No space for metadata")

    with monkeypatch.context() as patch:
        patch.setattr(rsv.json, "dump", fail_write)
        with pytest.raises(ReservationSetupError), rsv.reserve("camera"):
            pass
    assert contender("camera", lock_dir).returncode == 0


def test_reservation_reopens_existing_file_under_protected_regular(
    lock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_dir.mkdir(mode=0o1777)
    path = lock_dir / "camera.lock"
    path.touch(mode=0o666)
    inode = path.stat().st_ino
    original_open = os.open

    def protected_open(
        file: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if flags & os.O_CREAT:
            raise PermissionError(errno.EACCES, "Creation denied by protected_regular")
        return original_open(file, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(rsv.os, "open", protected_open)
    with rsv.reserve("camera"):
        assert contender("camera", lock_dir).returncode == 23
    assert path.stat().st_ino == inode
    assert contender("camera", lock_dir).returncode == 0


def test_sdk_imports_without_posix_only_modules() -> None:
    blocked = "import sys\nfor name in ('fcntl', 'pwd', 'grp'):\n    sys.modules[name] = None\nimport palmimo_sdk\n"
    result = subprocess.run([sys.executable, "-c", blocked], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("stage", ["existing", "publication"])
def test_reservation_rejects_symlink_without_overwriting_target(
    lock_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    lock_dir.mkdir()
    target = tmp_path / "victim"
    target.write_text("precious data")
    path = lock_dir / "camera.lock"
    if stage == "existing":
        path.symlink_to(target)
    else:

        def replace_publication(source: object, destination: Path) -> None:
            destination.symlink_to(target)
            raise FileExistsError()

        monkeypatch.setattr(rsv.os, "link", replace_publication)
    with pytest.raises(ReservationSetupError), rsv.reserve("camera"):
        pass
    assert target.read_text() == "precious data"


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_reservation_rejects_nonregular_lock(lock_dir: Path, kind: str) -> None:
    lock_dir.mkdir()
    path = lock_dir / "camera.lock"
    if kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(ReservationSetupError), rsv.reserve("camera"):
        pass


def test_reservation_rejects_symlink_directory(lock_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "other"
    target.mkdir()
    lock_dir.symlink_to(target, target_is_directory=True)
    with pytest.raises(ReservationSetupError), rsv.reserve("camera"):
        pass
    assert not list(target.iterdir())


def test_reservation_publishes_only_shared_directory(lock_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_mkdir = Path.mkdir
    observed: list[int] = []

    def observe_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        original_mkdir(path, *args, **kwargs)
        if lock_dir.exists():
            observed.append(stat.S_IMODE(lock_dir.stat().st_mode))

    monkeypatch.setattr(Path, "mkdir", observe_mkdir)
    previous = os.umask(0o077)
    try:
        with rsv.reserve("camera"):
            observed.append(stat.S_IMODE(lock_dir.stat().st_mode))
    finally:
        os.umask(previous)
    assert observed and set(observed) == {0o1777}
