"""Tests for :mod:`palmimo_companion_agent.realtime.services.reflexes` -- ReflexRunner + its notify wiring.

No camera, no VisionWatch driven for real: these exercise the notify
callback and :meth:`~palmimo_companion_agent.realtime.services.reflexes.ReflexRunner.settle`
directly, the pieces the bounded-pending-set / logged-failure fix touches.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from palmimo_companion_agent.realtime.log import EventLog
from palmimo_companion_agent.realtime.protocol import ItemCreate
from palmimo_companion_agent.realtime.services.reflexes import ReflexRunner, _build_notify


class _RecordingLog(EventLog):
    def __init__(self) -> None:
        super().__init__(None)
        self.entries: list[tuple[str, dict]] = []

    def write(self, kind: str, **fields: object) -> None:
        self.entries.append((kind, fields))


async def test_notify_sends_the_text_and_logs_it(fake_client: type) -> None:
    client = fake_client()
    log = _RecordingLog()
    pending: set[asyncio.Task[None]] = set()
    notify = _build_notify(client, log, pending)

    notify("[reflex] waved back at someone on the left.")
    await asyncio.wait(pending) if pending else None

    sent = [e for e in client.sent if isinstance(e, ItemCreate)]
    assert len(sent) == 1
    assert sent[0].item["content"][0]["text"] == "[reflex] waved back at someone on the left."
    assert log.entries == [("reflex", {"text": "[reflex] waved back at someone on the left."})]


async def test_notify_tasks_are_bounded_by_the_pending_set(fake_client: type) -> None:
    client = fake_client()
    log = _RecordingLog()
    pending: set[asyncio.Task[None]] = set()
    notify = _build_notify(client, log, pending)

    notify("first")
    notify("second")
    assert len(pending) <= 2  # both may still be pending right after scheduling
    await asyncio.sleep(0)  # let them run to completion
    await asyncio.sleep(0)

    assert pending == set(), "a finished notify-send task was never discarded from the pending set"


async def test_a_failing_notify_send_is_reported_not_silently_dropped(fake_client: type) -> None:
    class _FailingClient:
        async def send(self, event: object) -> None:
            raise RuntimeError("socket is gone")

        def events(self) -> object:
            raise NotImplementedError

    log = _RecordingLog()
    pending: set[asyncio.Task[None]] = set()
    notify = _build_notify(_FailingClient(), log, pending)

    notify("this send will fail")
    for task in list(pending):
        with contextlib.suppress(RuntimeError):
            await task

    assert pending == set(), "a failed notify-send task was never discarded"


# ----------------------------------------------------------------------
# ReflexRunner.settle -- cancels and reaps notify-send tasks the run()
# TaskGroup cancellation never touches (they aren't its children)
# ----------------------------------------------------------------------


async def test_settle_cancels_and_awaits_pending_notify_tasks() -> None:
    pending: set[asyncio.Task[None]] = set()

    async def _never_finishes() -> None:
        await asyncio.sleep(30)

    task = asyncio.ensure_future(_never_finishes())
    pending.add(task)
    await asyncio.sleep(0)  # let it start

    runner = ReflexRunner(watch=object(), engine=object(), notify_tasks=pending)
    await runner.settle(1.0)

    assert task.cancelled() or task.done()


async def test_settle_does_not_wait_forever_on_an_uncancellable_notify_task() -> None:
    pending: set[asyncio.Task[None]] = set()

    async def _ignores_cancellation() -> None:
        for _ in range(2):
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(30)

    task = asyncio.ensure_future(_ignores_cancellation())
    pending.add(task)
    await asyncio.sleep(0)

    runner = ReflexRunner(watch=object(), engine=object(), notify_tasks=pending)
    started = time.monotonic()
    await runner.settle(0.05)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"settle waited {elapsed:.1f}s on a notify task that would not settle"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_settle_with_no_pending_tasks_is_a_no_op() -> None:
    runner = ReflexRunner(watch=object(), engine=object(), notify_tasks=set())
    await runner.settle(1.0)  # must not raise
