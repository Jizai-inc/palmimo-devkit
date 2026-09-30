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


def test_reservation_creates_group_writable_file_despite_umask(lock_dir: Path) -> None:
    previous = os.umask(0o077)
    try:
        with rsv.reserve("servo_bus"):
            assert stat.S_IMODE((lock_dir / "servo_bus.lock").stat().st_mode) == 0o660
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


@pytest.mark.parametrize("choice", ["override", "system", "development"])
def test_reservation_selects_lock_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, choice: str) -> None:
    runtime = tmp_path / "run" / "palmimo"
    system = runtime / "locks"
    override = tmp_path / "override"
    monkeypatch.setattr(rsv, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(rsv, "SYSTEM_LOCK_DIR", system)
    monkeypatch.setattr(rsv.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("PALMIMO_LOCK_DIR", raising=False)
    if choice in {"override", "system"}:
        system.mkdir(parents=True)
    if choice == "override":
        monkeypatch.setenv("PALMIMO_LOCK_DIR", str(override))
    expected = {"override": override, "system": system, "development": tmp_path / f"palmimo-locks-{os.getuid()}"}[
        choice
    ]
    with rsv.reserve("camera"):
        assert contender("camera", expected).returncode == 23


def test_reservation_rejects_missing_system_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "palmimo"
    runtime.mkdir()
    monkeypatch.delenv("PALMIMO_LOCK_DIR", raising=False)
    monkeypatch.setattr(rsv, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(rsv, "SYSTEM_LOCK_DIR", runtime / "locks")
    with pytest.raises(ReservationSetupError), rsv.reserve("camera"):
        pass


@pytest.mark.skipif(os.geteuid() == 0, reason="Root bypasses directory write permissions")
@pytest.mark.parametrize("choice", ["override", "system"])
def test_reservation_does_not_fallback_when_directory_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, choice: str
) -> None:
    directory = tmp_path / "palmimo" / "locks"
    directory.mkdir(parents=True)
    monkeypatch.setattr(rsv, "RUNTIME_DIR", directory.parent)
    monkeypatch.setattr(rsv, "SYSTEM_LOCK_DIR", directory)
    monkeypatch.delenv("PALMIMO_LOCK_DIR", raising=False)
    if choice == "override":
        monkeypatch.setenv("PALMIMO_LOCK_DIR", str(directory))
    directory.chmod(0o550)
    try:
        with pytest.raises(ReservationSetupError) as caught, rsv.reserve("camera"):
            pass
        assert caught.value.remediation in {"join_group", "update_platform"}
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
        path.chmod(0o660)


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


@pytest.mark.parametrize("ending", ["normal", "sigkill"])
def test_reservation_never_publishes_incomplete_file_permissions(lock_dir: Path, ending: str) -> None:
    script = """
import os, sys
import palmimo_sdk.reservation as rsv
os.umask(0o027)
original = os.fchmod
def paused(fd, mode):
    print('ready', flush=True)
    sys.stdin.readline()
    original(fd, mode)
rsv.os.fchmod = paused
with rsv.reserve('camera'):
    pass
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PALMIMO_LOCK_DIR": str(lock_dir)},
    )
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10)
        assert process.stdout.readline().strip() == "ready"
        path = lock_dir / "camera.lock"
        assert not path.exists() or stat.S_IMODE(path.stat().st_mode) == 0o660
        if ending == "sigkill":
            process.kill()
        else:
            assert process.stdin is not None
            process.stdin.write("\n")
            process.stdin.flush()
        process.wait(timeout=10)
        if ending == "normal":
            assert process.returncode == 0
            assert stat.S_IMODE(path.stat().st_mode) == 0o660
        else:
            assert not path.exists() or stat.S_IMODE(path.stat().st_mode) == 0o660
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.parametrize("group,remediation", [("palmimo-locks", "join_group"), ("palmimo-apps", "update_platform")])
@pytest.mark.parametrize("target", ["directory", "file"])
def test_reservation_setup_error_identifies_remediation(
    lock_dir: Path, monkeypatch: pytest.MonkeyPatch, group: str, remediation: str, target: str
) -> None:
    import grp
    from types import SimpleNamespace

    lock_dir.mkdir()
    if target == "file":
        (lock_dir / "camera.lock").touch()
    monkeypatch.setattr(grp, "getgrgid", lambda _: SimpleNamespace(gr_name=group))
    if target == "directory":
        monkeypatch.setattr(rsv.os, "access", lambda *args: False)
    else:

        def denied(*args: object, **kwargs: object) -> int:
            raise PermissionError(errno.EACCES, "Cannot open lock")

        monkeypatch.setattr(rsv.os, "open", denied)
    with pytest.raises(ReservationSetupError) as caught, rsv.reserve("camera"):
        pass
    assert caught.value.remediation == remediation
    assert caught.value.group == group
