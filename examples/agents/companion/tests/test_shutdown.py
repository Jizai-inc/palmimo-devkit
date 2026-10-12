"""Exercise stop delivery in child processes, including uv-style duplicate signals."""

from __future__ import annotations

import os
import pty
import queue
import select
import signal
import subprocess
import sys
import termios
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def _probe(phase: str, sig: signal.Signals | None, frontend: str) -> tuple[int, str, float]:
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("run_shutdown_probe.py")), phase, frontend],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=os.environ.copy(),
    )
    assert process.stdout is not None and process.stdin is not None
    lines: list[str] = []
    received: queue.Queue[str] = queue.Queue()

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.append(line)
            received.put(line)
        received.put("")

    reader = threading.Thread(target=read, daemon=True)
    reader.start()

    def wait_for(marker: str) -> None:
        deadline = time.monotonic() + 10.0
        while True:
            try:
                line = received.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty:
                pytest.fail(f"probe did not reach {marker}: {''.join(lines)}")
            if not line:
                pytest.fail(f"probe exited before {marker}: {''.join(lines)}")
            if line.strip() == f"probe:{marker}":
                return

    try:
        if sig is None:
            process.stdin.close()
            started = time.monotonic()
            code = process.wait(timeout=10.0)
            reader.join(timeout=2.0)
            assert not reader.is_alive(), "probe output reader did not finish"
            return code, "".join(lines), time.monotonic() - started
        if phase == "session":
            process.stdin.close()
        wait_for(phase)
        if phase == "session":
            time.sleep(0.05)
            assert "probe:cleanup\n" not in lines, "stdin EOF ended a --no-stdin session"
        started = time.monotonic()
        process.send_signal(sig)
        if phase == "session":
            wait_for("cleanup")
        else:
            time.sleep(0.03)
        if process.poll() is None:
            process.send_signal(sig)
        if phase in ("build", "connect") and process.poll() is None:
            try:
                process.stdin.write("continue\n")
                process.stdin.flush()
            except BrokenPipeError:
                pass
        code = process.wait(timeout=10.0)
        elapsed = time.monotonic() - started
        reader.join(timeout=2.0)
        assert not reader.is_alive(), "probe output reader did not finish"
        return code, "".join(lines), elapsed
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2.0)
        reader.join(timeout=2.0)
        process.stdin.close()
        process.stdout.close()


@pytest.mark.parametrize(
    ("phase", "sig"),
    [
        ("build", signal.SIGTERM),
        ("build", signal.SIGINT),
        ("connect", signal.SIGTERM),
        ("connect", signal.SIGINT),
        ("session", signal.SIGTERM),
        ("session", signal.SIGHUP),
        ("session", signal.SIGINT),
    ],
    ids=[
        "build-term",
        "build-int",
        "connect-term",
        "connect-int",
        "session-term",
        "session-hup",
        "cleanup-int",
    ],
)
def test_frontends_stop_without_leaving_torque_on(phase: str, sig: signal.Signals) -> None:
    frontends = ["cli", "realtime", "tui"] if phase != "session" else ["cli", "realtime"]
    with ThreadPoolExecutor(max_workers=3) as executor:
        probes = [executor.submit(_probe, phase, sig, frontend) for frontend in frontends]
        for probe in probes:
            _assert_stopped(phase, sig, probe.result())


def _assert_stopped(phase: str, sig: signal.Signals, result: tuple[int, str, float]) -> None:
    code, output, elapsed = result
    assert code == 0, output
    assert elapsed < 10.0, output
    if phase == "build":
        assert "probe:connect" not in output, output
        assert "park complete" not in output, output
    else:
        assert "park complete" in output, output
        assert output.index("probe:connect finished") < output.index("probe:torque off"), output
        assert "probe:log closed" in output, output
        if phase == "connect":
            assert "probe:session" not in output, output
        if sig == signal.SIGINT and phase == "session":
            assert "probe:cleanup finished" not in output, output


@pytest.mark.parametrize(
    ("frontend", "phase"),
    [("realtime", "connect-error"), ("realtime", "network-error"), ("tui", "connect-error")],
)
def test_frontend_parks_when_startup_fails(frontend: str, phase: str) -> None:
    code, output, elapsed = _probe(phase, None, frontend)
    assert code != 0, output
    assert elapsed < 10.0, output
    assert "park complete" in output, output
    assert "probe:torque off" in output, output
    assert "probe:log closed" in output, output
    assert "probe:session" not in output, output


@pytest.mark.parametrize("action", ["term", "hup", "int", "exit", "ctrl-q"])
def test_tui_restores_terminal_and_parks(action: str) -> None:
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    event_read, event_write = os.pipe()
    env = {**os.environ, "TERM": "xterm-256color", "PROBE_EVENTS_FD": str(event_write)}
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("run_shutdown_probe.py")), "session", "tui"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        pass_fds=(event_write,),
        env=env,
        start_new_session=True,
    )
    os.close(event_write)
    streams = [master, event_read]
    screen = bytearray()
    logs = bytearray()

    def pump(timeout: float) -> None:
        for fd in select.select(streams, [], [], timeout)[0]:
            data = os.read(fd, 65536)
            if not data:
                streams.remove(fd)
            elif fd == master:
                screen.extend(data)
            else:
                logs.extend(data)

    def wait_for(marker: bytes) -> None:
        deadline = time.monotonic() + 10.0
        while marker not in logs:
            assert time.monotonic() < deadline and process.poll() is None, (screen + logs).decode(errors="replace")
            pump(0.05)

    try:
        wait_for(b"probe:session\n")
        during = termios.tcgetattr(slave)
        assert not during[3] & (termios.ECHO | termios.ICANON)
        if action in ("exit", "ctrl-q"):
            if action == "ctrl-q":
                os.write(master, b"\x03")
                pump(0.05)
                assert process.poll() is None and b"probe:cleanup\n" not in logs
            os.write(master, b"/exit\r" if action == "exit" else b"\x11")
        else:
            sig = {"term": signal.SIGTERM, "hup": signal.SIGHUP, "int": signal.SIGINT}[action]
            process.send_signal(sig)
        wait_for(b"probe:cleanup\n")
        assert termios.tcgetattr(slave) == before
        if action not in ("exit", "ctrl-q"):
            process.send_signal(sig)
        deadline = time.monotonic() + 10.0
        while process.poll() is None:
            assert time.monotonic() < deadline, (screen + logs).decode(errors="replace")
            pump(0.05)
        for _ in range(3):
            pump(0.02)
        assert process.returncode == 0, (screen + logs).decode(errors="replace")
        assert termios.tcgetattr(slave) == before
        assert b"\x1b[?1049h" in screen and b"\x1b[?1049l" in screen
        assert screen.rfind(b"\x1b[?25h") > screen.rfind(b"\x1b[?25l")
        assert b"park complete" in screen, (screen + logs).decode(errors="replace")
        assert b"probe:torque off" in logs
        assert b"probe:log closed" in logs
        if action == "int":
            assert b"probe:cleanup finished" not in logs
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2.0)
        os.close(event_read)
        os.close(master)
        os.close(slave)
