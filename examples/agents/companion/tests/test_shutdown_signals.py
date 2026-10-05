"""Process-level shutdown checks without hardware or external services."""

from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from palmimo_sdk.shutdown import STOP_SIGNALS


_CHILD = textwrap.dedent("""\
    import asyncio
    import sys
    import time
    from pathlib import Path
    from types import SimpleNamespace

    from palmimo_companion_agent.pipeline.settings import PipelineSettings
    from palmimo_companion_agent.pipeline.ui import cli, tui

    directory = Path(sys.argv[1])
    frontend = sys.argv[2]

    class Runtime:
        history = SimpleNamespace(subscribe=lambda callback: None)
        conductor = SimpleNamespace(submit_user_text=lambda text: None)

        async def connect(self):
            pass

        async def start(self):
            (directory / "ready").touch()

        async def aclose(self):
            (directory / "closing").touch()
            await asyncio.sleep(0.5)
            (directory / "parked").touch()

    runtime = Runtime()
    settings = PipelineSettings.with_env_file(None).model_copy(update={"hardware": False})
    if frontend == "cli":
        cli.build_runtime = lambda settings: runtime
        asyncio.run(cli.run_cli(settings, read_stdin=False))
    elif frontend == "tui":
        tui.build_runtime = lambda settings: runtime
        class App(tui.CompanionAgentApp):
            def on_mount(self):
                self.run_worker(self.runtime.start())
        tui.CompanionAgentApp = lambda runtime: App(runtime)
        tui.run_tui(settings)
    else:
        from palmimo_companion_agent.realtime.app import RealtimeSession, _wake_and_disconnect
        from palmimo_companion_agent.realtime.state import Sleeping

        class Hardware:
            def wake(self):
                (directory / "ready").touch()
                (directory / "closing").touch()
                time.sleep(0.5)

            def disconnect(self, park=True):
                time.sleep(0.5)
                (directory / "parked").touch()

        class Service:
            async def run(self):
                (directory / "ready").touch()
                await asyncio.Event().wait()

        class Watch:
            async def aclose(self):
                pass

        class Bridge:
            async def settle(self, timeout):
                (directory / "closing").touch()
                await asyncio.sleep(0.5)

        sleeping = Sleeping()
        sleeping.asleep = True
        if frontend == "realtime-fallback":
            asyncio.run(_wake_and_disconnect(Hardware(), sleeping))
        else:
            session = RealtimeSession(
                client=None, services=[Service()], bridge=Bridge(),
                playback=SimpleNamespace(close=lambda: None), watch=Watch(),
                palmimo=Hardware(), sleeping=sleeping,
                usage=SimpleNamespace(report=lambda elapsed: ""), frame=SimpleNamespace(pushed=0),
            )
            asyncio.run(session.run(30))
""")


def _wait_for_marker(process: subprocess.Popen[str], marker: Path) -> None:
    deadline = time.monotonic() + 30
    while not marker.exists():
        assert process.poll() is None, f"child exited with {process.returncode} before {marker.name}"
        assert time.monotonic() < deadline, f"timed out waiting for {marker.name}"
        time.sleep(0.01)


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX process signals")
@pytest.mark.parametrize("frontend", ["cli", "tui", "realtime", "realtime-fallback"])
@pytest.mark.parametrize("stop_signal", STOP_SIGNALS)
@pytest.mark.parametrize("repeat", [False, True], ids=["single", "repeated"])
def test_frontend_parks_before_exiting_on_stop_signals(
    tmp_path: Path, frontend: str, stop_signal: signal.Signals, repeat: bool
) -> None:
    with subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(tmp_path), frontend],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            _wait_for_marker(process, tmp_path / "ready")
            process.send_signal(stop_signal)
            _wait_for_marker(process, tmp_path / "closing")
            if repeat:
                process.send_signal(stop_signal)
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stdout + stderr
            assert (tmp_path / "parked").exists()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
