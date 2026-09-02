"""Tests for :mod:`palmimo_companion_agent.realtime.app` -- RealtimeSession's shutdown order and signal handling.

Shutdown is a method on :class:`RealtimeSession` that delegates in-flight
tool settling to :class:`~..bridge.ToolBridge.settle` instead of cancelling
a bare task list itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, cast

from palmimo_companion_agent.realtime import app as app_module
from palmimo_companion_agent.realtime.app import RealtimeSession
from palmimo_companion_agent.realtime.bridge import ToolBridge
from palmimo_companion_agent.realtime.log import EventLog
from palmimo_companion_agent.realtime.services.audio import Playback
from palmimo_companion_agent.realtime.services.frames import LiveFrame
from palmimo_companion_agent.realtime.services.reflexes import ReflexRunner
from palmimo_companion_agent.realtime.services.router import _SessionClosed
from palmimo_companion_agent.realtime.state import Sleeping
from palmimo_sdk import Palmimo
from palmimo_sdk.agent.toolset import AgentToolSet


class _Recorder:
    """Records the order of the shutdown steps that touch hardware/audio."""

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
        frame=cast(LiveFrame, LiveFrame(camera=None)),
    )

    await session.run(30.0)  # must return normally, not raise, and not wait out the 30s timeout

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
    """Mirrors _run()'s own outer `finally`: a failure between Palmimo.connect() succeeding
    and a RealtimeSession ever being built (most commonly RealtimeClient.connect() itself,
    on a bad key or no network) must still park the robot -- this is the function that guard
    calls, so exercising it directly proves the fix without needing a live websocket/hardware."""
    recorder = _Recorder()

    await app_module._wake_and_disconnect(cast(Palmimo, recorder), Sleeping())

    assert recorder.order == ["disconnect"]
    assert recorder.park is True


async def test_the_signal_handlers_are_installed_for_the_session_and_given_back() -> None:
    """Installing one for SIGINT replaces the KeyboardInterrupt disposition, so leaving it in
    place through a multi-second shutdown swallows the operator's second and third Ctrl+C."""

    class _Loop:
        def __init__(self) -> None:
            self.added: list[Any] = []
            self.removed: list[Any] = []

        def add_signal_handler(self, sig: Any, cb: Any) -> None:
            self.added.append(sig)

        def remove_signal_handler(self, sig: Any) -> None:
            self.removed.append(sig)

    loop = _Loop()

    async def _drive() -> None:
        with app_module._signal_stop(asyncio.Event()):
            assert loop.added, "no handler was installed"
            assert not loop.removed

    import unittest.mock

    with unittest.mock.patch.object(asyncio, "get_running_loop", return_value=loop):
        await _drive()

    assert loop.added == loop.removed, "a signal handler outlived the session"


async def test_signal_handlers_are_removed_before_shutdown_runs() -> None:
    """Nesting _shutdown inside _signal_stop's `with` would keep the handler installed for the
    whole shutdown -- exactly the multi-second stretch a stuck robot most needs a 2nd/3rd
    Ctrl+C to interrupt. Runs a real RealtimeSession.run() end to end (timing out almost
    immediately, no services) and checks the handler-removal events precede _shutdown's."""
    events: list[str] = []

    class _OrderRecorder(_Recorder):
        async def settle(self, timeout: float) -> None:
            events.append("bridge.settle")
            await super().settle(timeout)

    recorder = _OrderRecorder()
    session = RealtimeSession(
        client=cast(Any, None),
        services=[],
        bridge=cast(ToolBridge, recorder),
        playback=cast(Playback, recorder),
        watch=cast(Any, recorder),
        palmimo=cast(Palmimo, recorder),
        sleeping=Sleeping(),
        usage=cast(Any, app_module.Usage("gpt-realtime-2.1")),
        frame=cast(LiveFrame, LiveFrame(camera=None)),
    )

    loop = asyncio.get_running_loop()

    def fake_add(sig: Any, cb: Any) -> None:
        events.append(f"add:{sig}")

    def fake_remove(sig: Any) -> None:
        events.append(f"remove:{sig}")

    import unittest.mock

    with (
        unittest.mock.patch.object(loop, "add_signal_handler", side_effect=fake_add, create=True),
        unittest.mock.patch.object(loop, "remove_signal_handler", side_effect=fake_remove, create=True),
    ):
        await session.run(0.01)  # times out almost immediately -- no services to wait on

    assert any(e.startswith("add:") for e in events), "no signal handler was ever installed"
    remove_indices = [i for i, e in enumerate(events) if e.startswith("remove:")]
    settle_index = events.index("bridge.settle")
    assert remove_indices, "no signal handler was ever removed"
    assert all(i < settle_index for i in remove_indices), "shutdown began before the signal handlers were removed"


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
