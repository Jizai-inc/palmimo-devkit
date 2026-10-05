"""Process-level shutdown checks without hardware or external services."""

from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


_CHILD = textwrap.dedent("""\
    import asyncio
    import sys
    import time
    import threading
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
            await asyncio.sleep(0.1)
            (directory / "parked").touch()

    runtime = Runtime()
    settings = PipelineSettings.with_env_file(None).model_copy(update={"hardware": False})
    if frontend == "cli":
        cli.build_runtime = lambda settings: runtime
        asyncio.run(cli.run_cli(settings, read_stdin=False))
    elif frontend in ("tui", "tui-terminal"):
        tui.build_runtime = lambda settings: runtime
        class App(tui.CompanionAgentApp):
            def on_mount(self):
                if frontend == "tui-terminal":
                    def close():
                        raise OSError("terminal disconnected")
                    self._driver.close = close
                self.run_worker(self.runtime.start())
        tui.CompanionAgentApp = lambda runtime: App(runtime)
        tui.run_tui(settings)
    else:
        from palmimo_companion_agent.realtime.app import RealtimeSession
        from palmimo_companion_agent.realtime.state import Sleeping

        class Hardware:
            has_connectable_resource = True
            is_connected = True
            camera = None

            def __init__(self):
                self.connected = threading.Event()
                if frontend != "realtime-connect":
                    self.connected.set()

            def connect(self):
                if frontend == "realtime-connect":
                    (directory / "ready").touch()
                    time.sleep(0.1)
                    (directory / "connected").touch()
                    self.connected.set()

            def wake(self):
                time.sleep(0.1)

            def disconnect(self, park=True):
                self.connected.wait()
                if frontend == "realtime-connect":
                    assert (directory / "connected").exists(), "park raced with connect"
                (directory / "closing").touch()
                time.sleep(0.1)
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
                await asyncio.sleep(0.1)

        sleeping = Sleeping()
        sleeping.asleep = True
        if frontend in ("realtime-fallback", "realtime-connect", "realtime-update"):
            import os
            import contextlib
            from palmimo_companion_agent.realtime import app
            from palmimo_companion_agent.realtime.settings import RealtimeSettings
            os.environ["OPENAI_API_KEY"] = "test"
            hardware = Hardware()
            app.load_settings = lambda **kwargs: RealtimeSettings()
            app.build_robot = lambda settings: (hardware, None)
            app.FaceLocator = lambda: None
            app.build_toolset = lambda *args: None
            app.ToolView = lambda *args, **kwargs: SimpleNamespace(to_openai_tools=lambda: [])
            app.build_vision = lambda *args: Watch()
            @contextlib.asynccontextmanager
            async def connect(**kwargs):
                if frontend == "realtime-update":
                    class Client:
                        async def send(self, message):
                            (directory / "ready").touch()
                            await asyncio.Event().wait()
                    yield Client()
                else:
                    (directory / "ready").touch()
                    await asyncio.Event().wait()
                    yield None
            app.RealtimeClient.connect = connect
            asyncio.run(app._run(app._build_parser().parse_args(["--model", "gpt-realtime", "--voice", "coral"])))
        else:
            session = RealtimeSession(
                client=None, services=[Service()], bridge=Bridge(),
                playback=SimpleNamespace(close=lambda: None), watch=Watch(),
                palmimo=Hardware(), sleeping=sleeping,
                usage=SimpleNamespace(report=lambda elapsed: ""), frame=SimpleNamespace(pushed=0),
            )
            from palmimo_companion_agent.shutdown import stop_scope
            async def run():
                stop = asyncio.Event()
                async def cleanup():
                    pass
                async with stop_scope(cleanup, stop.set):
                    await session.run(30, stop=stop)
            asyncio.run(run())
""")


def _wait_for_marker(process: subprocess.Popen[str], marker: Path) -> None:
    deadline = time.monotonic() + 30
    while not marker.exists():
        assert process.poll() is None, f"child exited with {process.returncode} before {marker.name}"
        assert time.monotonic() < deadline, f"timed out waiting for {marker.name}"
        time.sleep(0.01)


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX process signals")
@pytest.mark.parametrize(
    ("frontend", "stop_signal"),
    [
        (name, signal.SIGTERM)
        for name in ("cli", "tui", "realtime", "realtime-fallback", "realtime-connect", "realtime-update")
    ]
    + [("cli", signal.SIGINT)]
    + ([("tui-terminal", signal.SIGHUP)] if hasattr(signal, "SIGHUP") else []),
)
def test_frontend_parks_before_exiting_on_stop_signals(
    tmp_path: Path, frontend: str, stop_signal: signal.Signals
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
            process.send_signal(stop_signal)
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stdout + stderr
            assert (tmp_path / "parked").exists()
            assert "Traceback" not in stderr
            assert "Task exception was never retrieved" not in stderr
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
