"""Exercise stop delivery in child processes, including uv-style duplicate signals."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
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


@pytest.mark.parametrize("frontend", ["cli", "realtime"])
@pytest.mark.parametrize(
    ("phase", "sig"),
    [
        ("build", signal.SIGTERM),
        ("build", signal.SIGINT),
        ("connect", signal.SIGTERM),
        ("connect", signal.SIGHUP),
        ("connect", signal.SIGINT),
        ("session", signal.SIGTERM),
        ("session", signal.SIGHUP),
        ("session", signal.SIGINT),
    ],
    ids=[
        "build-term",
        "build-int",
        "connect-term",
        "connect-hup",
        "connect-int",
        "session-term",
        "session-hup",
        "cleanup-int",
    ],
)
def test_frontend_stops_without_leaving_torque_on(phase: str, sig: signal.Signals, frontend: str) -> None:
    code, output, elapsed = _probe(phase, sig, frontend)
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


@pytest.mark.parametrize("phase", ["connect-error", "network-error"])
def test_realtime_parks_when_startup_fails(phase: str) -> None:
    code, output, elapsed = _probe(phase, None, "realtime")
    assert code != 0, output
    assert elapsed < 10.0, output
    assert "park complete" in output, output
    assert "probe:torque off" in output, output
    assert "probe:log closed" in output, output
    assert "probe:session" not in output, output
