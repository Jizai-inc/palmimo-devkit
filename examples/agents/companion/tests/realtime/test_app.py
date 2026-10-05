"""Tests for :mod:`palmimo_companion_agent.realtime.app` -- RealtimeSession's shutdown order and signal handling.

Shutdown is a method on :class:`RealtimeSession` that delegates in-flight
tool settling to :class:`~..bridge.ToolBridge.settle` instead of cancelling
a bare task list itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from typing import Any, cast

import pytest

from palmimo_companion_agent.realtime import app as app_module
from palmimo_companion_agent.realtime.app import RealtimeSession
from palmimo_companion_agent.realtime.bridge import ToolBridge
from palmimo_companion_agent.realtime.log import EventLog
from palmimo_companion_agent.realtime.services.audio import Playback
from palmimo_companion_agent.realtime.services.frames import LiveFrame
from palmimo_companion_agent.realtime.services.reflexes import ReflexRunner
from palmimo_companion_agent.realtime.services.router import _SessionClosed
from palmimo_companion_agent.realtime.state import Sleeping
from palmimo_companion_agent.shutdown import stop_scope
from palmimo_sdk import Palmimo
from palmimo_sdk.agent.toolset import AgentToolSet


class _Recorder:
    """Records the order of the shutdown steps that touch hardware/audio."""

    has_connectable_resource = True
    is_connected = True

    def __init__(self) -> None:
        self.order: list[str] = []
        self.park: bool | None = None

    # ToolBridge
    async def settle(self, timeout: float) -> None:
        self.order.append("bridge.settle")

    # Playback
    def close(self) -> None:
        self.order.append("playback.close")

    # VisionWatch
    async def aclose(self) -> None:
        self.order.append("watch.aclose")

    # Palmimo
    def wake(self) -> None:
        self.order.append("wake")

    def disconnect(self, park: bool = True) -> None:
        self.park = park
        self.order.append("disconnect")


def _session(recorder: _Recorder, *, asleep: bool) -> RealtimeSession:
    sleeping = Sleeping()
    sleeping.asleep = asleep
    return RealtimeSession(
        client=cast(Any, None),
        services=[],
        bridge=cast(ToolBridge, recorder),
        playback=cast(Playback, recorder),
        watch=cast(Any, recorder),
        palmimo=cast(Palmimo, recorder),
        sleeping=sleeping,
        usage=cast(Any, None),
        frame=cast(LiveFrame, None),
    )


async def test_a_closed_socket_ends_the_session_cleanly_and_still_parks() -> None:
    """The router raising _SessionClosed (see that exception's own docstring: a TaskGroup does
    not cancel siblings just because one task returns normally) must be swallowed by run()'s own
    except* -- not propagate out -- and _shutdown must still run."""

    class _ClosesImmediately:
        name = "router"

        async def run(self) -> None:
            raise _SessionClosed

    recorder = _Recorder()
    session = RealtimeSession(
        client=cast(Any, None),
        services=[_ClosesImmediately()],
        bridge=cast(ToolBridge, recorder),
        playback=cast(Playback, recorder),
        watch=cast(Any, recorder),
        palmimo=cast(Palmimo, recorder),
        sleeping=Sleeping(),
        usage=cast(Any, app_module.Usage("gpt-realtime-2.1")),
        frame=LiveFrame(camera=None),
    )

    await session.run(30.0, stop=asyncio.Event())  # must return normally, not raise, and not wait out the 30s timeout

    assert recorder.order == ["bridge.settle", "playback.close", "watch.aclose", "disconnect"]


async def test_shutdown_settles_tool_work_before_disconnecting() -> None:
    recorder = _Recorder()

    await _session(recorder, asleep=False)._shutdown()

    assert recorder.order == ["bridge.settle", "playback.close", "watch.aclose", "disconnect"]
    assert recorder.park is True


async def test_shutdown_settles_reflex_notify_tasks_between_bridge_and_playback() -> None:
    """The reflex engine's notify-send tasks are not children of the reflex service's own task
    (see ReflexRunner.settle's docstring), so _shutdown must settle them explicitly -- ordered
    alongside the bridge's own tool-task settle, before audio/hardware teardown."""
    recorder = _Recorder()

    class _ReflexRecorder:
        async def settle(self, timeout: float) -> None:
            recorder.order.append("reflexes.settle")

    session = RealtimeSession(
        client=cast(Any, None),
        services=[],
        bridge=cast(ToolBridge, recorder),
        playback=cast(Playback, recorder),
        watch=cast(Any, recorder),
        palmimo=cast(Palmimo, recorder),
        sleeping=Sleeping(),
        usage=cast(Any, None),
        frame=cast(LiveFrame, None),
        reflexes=cast(ReflexRunner, _ReflexRecorder()),
    )

    await session._shutdown()

    assert recorder.order == ["bridge.settle", "reflexes.settle", "playback.close", "watch.aclose", "disconnect"]


async def test_shutdown_without_a_reflex_runner_still_works() -> None:
    """reflexes=None (the default) must not be treated as a live ReflexRunner to settle."""
    recorder = _Recorder()

    await _session(recorder, asleep=False)._shutdown()

    assert recorder.order == ["bridge.settle", "playback.close", "watch.aclose", "disconnect"]


async def test_shutdown_wakes_a_sleeping_robot_before_parking_it() -> None:
    """disconnect() parks by streaming the stand-up pose; asleep means the legs are on the
    reduced-gain sleep() left them at, so the park would be seconds of a goal they cannot reach."""
    recorder = _Recorder()

    await _session(recorder, asleep=True)._shutdown()

    assert recorder.order == ["bridge.settle", "playback.close", "watch.aclose", "wake", "disconnect"]


async def test_shutdown_skips_the_park_when_waking_a_sleeping_robot_fails() -> None:
    class _WakeFails(_Recorder):
        def wake(self) -> None:
            self.order.append("wake")
            raise RuntimeError("the servo bus is gone")

    recorder = _WakeFails()

    await _session(recorder, asleep=True)._shutdown()

    assert recorder.order == ["bridge.settle", "playback.close", "watch.aclose", "wake", "disconnect"]
    assert recorder.park is False, "parked a robot that could not be woken"


async def test_wake_and_disconnect_parks_a_robot_that_never_became_a_session() -> None:
    """A failure after connect but before session creation must still park the robot."""
    recorder = _Recorder()

    await app_module._wake_and_disconnect(cast(Palmimo, recorder), Sleeping())

    assert recorder.order == ["disconnect"]
    assert recorder.park is True


async def test_bridge_settle_does_not_wait_forever_on_an_uncancellable_tool() -> None:
    """Palmimo.sleep()/wake() do not poll the cancel counter, so a tool inside one cannot be
    shortened. Holding the process past a service manager's stop timeout gets it SIGKILLed."""

    class _Toolset:
        async def cancel_running(self) -> None: ...

        def is_busy(self) -> bool:
            return False

        async def call(self, name: str, args: dict[str, Any]) -> Any: ...

    bridge = ToolBridge(cast(Any, None), cast(AgentToolSet, _Toolset()), Sleeping(), EventLog.open(None))

    async def _ignores_cancellation() -> None:
        for _ in range(2):
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(30)

    task = asyncio.ensure_future(_ignores_cancellation())
    bridge._tasks.add(task)
    await asyncio.sleep(0)  # let it start

    started = time.monotonic()
    await bridge.settle(0.05)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"settle waited {elapsed:.1f}s on a tool that would not settle"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="requires POSIX loop signal handlers")
async def test_session_keeps_outer_stop_handler_during_remaining_cleanup() -> None:
    recorder = _Recorder()
    session = _session(recorder, asleep=False)
    session._usage = cast(Any, app_module.Usage("gpt-realtime-2.1"))
    session._frame = LiveFrame(camera=None)

    async def cleanup() -> None:
        pass

    loop = asyncio.get_running_loop()
    async with stop_scope(cleanup):
        await session.run(0.01, stop=asyncio.Event())
        assert signal.SIGTERM in cast(Any, loop)._signal_handlers
