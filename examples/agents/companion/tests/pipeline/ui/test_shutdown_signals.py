"""Stop signals against ``run_cli`` and ``run_tui`` processes: the park must finish, and the exit must be clean.

Runs in a subprocess because the failure is the process being killed by a
signal's default action, which cannot be observed from inside it. The wiring
module is stubbed with a runtime whose teardown marks its start and then takes
a fixed time to "park" -- no hardware, network or LLM.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


_CHILD = """
import asyncio
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

workdir = Path(sys.argv[1])


class _Palmimo:
    def disconnect(self):
        time.sleep(0.5)
        (workdir / "parked").write_text("done")


class _Runtime:
    history = SimpleNamespace(subscribe=lambda callback: None)
    palmimo = _Palmimo()

    async def start(self):
        (workdir / "ready").touch()

    async def aclose(self):
        (workdir / "teardown").touch()
        await asyncio.to_thread(self.palmimo.disconnect)


wiring = types.ModuleType("palmimo_companion_agent.pipeline.wiring")
wiring.Runtime = _Runtime
wiring.build_runtime = lambda settings: _Runtime()
sys.modules[wiring.__name__] = wiring

from palmimo_companion_agent.pipeline.ui.cli import run_cli

asyncio.run(run_cli(None, read_stdin=False))
"""

_TUI_CHILD = """
import asyncio
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

workdir = Path(sys.argv[1])
stop_during_connect = len(sys.argv) > 2
sys.stderr.close()  # a hung-up terminal: the park report must not stand in the park's way


class _Palmimo:
    has_connectable_resource = True
    is_connected = True

    def disconnect(self, park=True):
        (workdir / "teardown").touch()
        time.sleep(0.5)
        (workdir / "parked").write_text("done")
        self.is_connected = False


class _Runtime:
    palmimo = _Palmimo()

    async def connect(self):
        if stop_during_connect:
            (workdir / "ready").touch()
            await asyncio.sleep(30)


wiring = types.ModuleType("palmimo_companion_agent.pipeline.wiring")
wiring.Runtime = _Runtime
wiring.build_runtime = lambda settings: _Runtime()
sys.modules[wiring.__name__] = wiring

import palmimo_companion_agent.pipeline.ui.tui as tui


class _App:
    def __init__(self, runtime):
        pass

    def run(self):
        (workdir / "ready").touch()
        time.sleep(30)


tui.CompanionAgentApp = _App
tui.run_tui(None)
"""

_DEADLINE_SECONDS = 15.0


def _wait_for(marker: Path, proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + _DEADLINE_SECONDS
    while not marker.exists():
        assert proc.poll() is None, f"child exited ({proc.returncode}) before {marker.name} appeared"
        assert time.monotonic() < deadline, f"timed out waiting for {marker.name}"
        time.sleep(0.01)


def _stop_and_check(child: str, tmp_path: Path, signum: signal.Signals, repeat: bool, *args: str) -> None:
    proc = subprocess.Popen([sys.executable, "-c", child, str(tmp_path), *args])
    try:
        _wait_for(tmp_path / "ready", proc)
        proc.send_signal(signum)
        if repeat:
            _wait_for(tmp_path / "teardown", proc)
            proc.send_signal(signum)
        returncode = proc.wait(timeout=_DEADLINE_SECONDS)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert (tmp_path / "parked").read_text() == "done"
    assert returncode == 0


@pytest.mark.parametrize("repeat", [False, True], ids=["once", "twice"])
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP], ids=lambda s: s.name)
def test_run_cli_parks_and_exits_zero_when_stop_signal_arrives(
    tmp_path: Path, signum: signal.Signals, repeat: bool
) -> None:
    _stop_and_check(_CHILD, tmp_path, signum, repeat)


# SIGINT is excluded: Textual owns the terminal and delivers Ctrl+C as a key press.
@pytest.mark.parametrize("repeat", [False, True], ids=["once", "twice"])
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP], ids=lambda s: s.name)
def test_run_tui_parks_and_exits_zero_when_stop_signal_arrives(
    tmp_path: Path, signum: signal.Signals, repeat: bool
) -> None:
    _stop_and_check(_TUI_CHILD, tmp_path, signum, repeat)


def test_run_tui_parks_and_exits_zero_when_stop_signal_arrives_during_connect(tmp_path: Path) -> None:
    _stop_and_check(_TUI_CHILD, tmp_path, signal.SIGTERM, False, "stop-during-connect")
