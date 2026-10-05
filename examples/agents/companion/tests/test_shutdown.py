"""Shutdown behavior when the terminal cannot accept diagnostic output."""

from __future__ import annotations

import sys
from typing import Any, cast

import pytest

from palmimo_companion_agent.pipeline.wiring import Runtime
from palmimo_companion_agent.realtime.app import _wake_and_disconnect
from palmimo_companion_agent.realtime.state import Sleeping
from palmimo_sdk import Palmimo


@pytest.mark.parametrize("frontend", ["pipeline", "realtime"])
@pytest.mark.parametrize("error", [OSError(5, "terminal disconnected"), ValueError("closed stream")])
async def test_cleanup_disconnects_when_stderr_write_fails(
    monkeypatch: pytest.MonkeyPatch, frontend: str, error: Exception
) -> None:
    class Stream:
        def write(self, text: str) -> int:
            raise error

    class Robot:
        has_connectable_resource = True
        is_connected = True

        def disconnect(self, *, park: bool = True) -> None:
            self.is_connected = False

    robot = Robot()
    monkeypatch.setattr(sys, "stderr", Stream())
    if frontend == "pipeline":
        runtime = Runtime(cast(Any, None), cast(Palmimo, robot), cast(Any, None), cast(Any, None))
        await runtime.aclose()
    else:
        await _wake_and_disconnect(cast(Palmimo, robot), Sleeping())
    assert not robot.is_connected
