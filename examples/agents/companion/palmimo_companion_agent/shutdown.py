"""Shutdown coordination shared by the companion front ends."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TextIO, cast

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


class ParkOutput:
    """Keep shutdown diagnostics best-effort when the terminal is gone."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = sys.stderr if stream is None else stream

    def write(self, text: str) -> int:
        try:
            return self._stream.write(text)
        except (OSError, ValueError):
            return len(text)

    def flush(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            self._stream.flush()

    def as_text_io(self) -> TextIO:
        """Expose the write/flush interface used by the SDK's park helper."""
        return cast(TextIO, self)


async def wait_until_done[T](future: asyncio.Future[T]) -> T:
    """Wait through caller cancellation, then return or raise the worker result."""
    while not future.done():
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(future)
    return future.result()
