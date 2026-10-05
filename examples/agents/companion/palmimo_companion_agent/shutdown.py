"""Signal delivery shared by the companion front ends."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

from palmimo_sdk.shutdown import StopRequest, loop_stop_on_signals


@contextlib.asynccontextmanager
async def stop_scope(
    cleanup: Callable[[], Awaitable[None]], on_stop: Callable[[], None] | None = None
) -> AsyncIterator[StopRequest]:
    """Deliver the first stop cooperatively; keep this scope around teardown.

    Without *on_stop*, cancel the calling task. A third-party runner should
    supply its own exit request instead. The yielded request records whether
    a signal arrived, including during teardown.
    """
    task = asyncio.current_task()
    assert task is not None
    stop = StopRequest()
    stopping = False

    def request_stop() -> None:
        nonlocal stopping
        # systemd signals the cgroup, and uv forwards the signal to Python too.
        # Duplicate delivery must not cancel the cleanup that releases torque.
        if not stopping:
            stopping = True
            if on_stop is None:
                task.cancel()
            else:
                on_stop()

    with loop_stop_on_signals(stop, request_stop):
        try:
            yield stop
        finally:
            stopping = True
            await cleanup()
