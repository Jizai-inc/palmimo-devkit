"""Run a companion front end with an in-memory servo bus for signal tests."""

from __future__ import annotations

import asyncio
import inspect
import io
import os
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


if source := os.environ.get("COMPANION_SHUTDOWN_SOURCE"):
    sys.path.insert(0, source)

from palmimo_companion_agent.main import run
from palmimo_companion_agent.pipeline import wiring
from palmimo_companion_agent.pipeline.settings import PipelineSettings
from palmimo_sdk import Palmimo, ServoDriver


PHASE = sys.argv[1]


def mark(phase: str) -> None:
    print(f"probe:{phase}", file=sys.stderr, flush=True)


class FakeDriver(ServoDriver):
    """An unsensed bus: SDK glides fall back to one write, with no physical dwell."""

    def __init__(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True
        mark("connect")
        if PHASE == "connect":
            sys.stdin.readline()
        mark("connect finished")

    def disconnect(self) -> None:
        if self._connected:
            self._connected = False
            mark("torque off")

    def _write_positions(self, positions: dict[str, int]) -> None:
        pass


class FakeLlm:
    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=None))])


class EventLog(io.StringIO):
    def close(self) -> None:
        super().close()
        mark("log closed")


build_runtime: Callable[..., wiring.Runtime] = wiring.build_runtime


def build_probe_runtime(settings: PipelineSettings) -> wiring.Runtime:
    driver = FakeDriver()
    with patch.object(wiring.LlmProvider, "from_settings", return_value=FakeLlm()):
        if "driver" in inspect.signature(build_runtime).parameters:
            runtime = build_runtime(settings, driver=driver)
        else:
            # Supply the same bus when exercising an entry point without the injection argument.
            with patch.object(wiring, "Palmimo", return_value=Palmimo(driver=driver)):
                runtime = build_runtime(settings)
    runtime.event_log = EventLog()
    original_run = runtime.conductor.run

    async def session() -> None:
        mark("session")
        try:
            await original_run()
        finally:
            mark("cleanup")
            await asyncio.sleep(0.8)
            mark("cleanup finished")

    runtime.conductor.run = session  # type: ignore[method-assign]
    if PHASE == "build":
        mark("build")
        sys.stdin.readline()
    return runtime


if __name__ == "__main__":
    os.environ["GEMINI_API_KEY"] = "test-key"
    os.environ["OPENAI_API_KEY"] = "test-key"
    sys.argv = [sys.argv[0], "--ui", "cli", "--no-hardware", "--no-stdin"]
    with patch("palmimo_companion_agent.pipeline.ui.cli.build_runtime", build_probe_runtime):
        run()
