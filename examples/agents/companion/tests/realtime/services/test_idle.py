"""Tests for :mod:`palmimo_companion_agent.realtime.services.idle` -- IdlePacer + IdleTicker."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from palmimo_companion_agent.realtime.log import EventLog
from palmimo_companion_agent.realtime.protocol import ResponseCreate
from palmimo_companion_agent.realtime.services.idle import IDLE_MAX_SECONDS, IDLE_MIN_SECONDS, IdlePacer, IdleTicker
from palmimo_companion_agent.realtime.state import Sleeping


def test_pacer_is_not_due_immediately_after_construction() -> None:
    pacer = IdlePacer()
    assert pacer.is_due() is False


def test_pacer_touch_draws_from_the_configured_window() -> None:
    pacer = IdlePacer()
    before = time.monotonic()
    pacer.touch()
    assert IDLE_MIN_SECONDS <= pacer._due - before <= IDLE_MAX_SECONDS + 0.01


def test_pacer_is_never_due_while_busy() -> None:
    pacer = IdlePacer()
    pacer._due = time.monotonic() - 1  # force "due"
    pacer.busy = True
    assert pacer.is_due() is False


async def _run_briefly(coro: Any, seconds: float = 0.05) -> None:
    task = asyncio.ensure_future(coro)
    await asyncio.sleep(seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _run_until(coro: Any, predicate: Any, timeout: float = 3.0) -> None:
    """Run *coro* as a background task until *predicate* is true, then cancel it.

    Polls rather than sleeping a fixed guess at how long one IdleTicker tick
    period (_TICK_PERIOD_S) takes -- a fixed sleep just past that period is
    exactly the kind of margin that flakes under CI scheduling jitter.
    """
    task = asyncio.ensure_future(coro)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition was never met")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_idle_ticker_never_fires_while_asleep(fake_client: type) -> None:
    client = fake_client()
    pacer = IdlePacer()
    pacer._due = time.monotonic() - 1  # would otherwise be immediately due
    sleeping = Sleeping()
    sleeping.asleep = True
    ticker = IdleTicker(client, pacer, sleeping, "idle prompt", [], EventLog.open(None))

    await _run_briefly(ticker.run())

    assert client.sent == []


async def test_idle_ticker_fires_a_tagged_response_create_when_due(fake_client: type) -> None:
    client = fake_client()
    pacer = IdlePacer()
    pacer._due = time.monotonic() - 1
    sleeping = Sleeping()
    ticker = IdleTicker(
        client, pacer, sleeping, "idle prompt", [{"type": "function", "name": "nod"}], EventLog.open(None)
    )

    await _run_until(ticker.run(), lambda: any(isinstance(e, ResponseCreate) for e in client.sent))

    creates = [e for e in client.sent if isinstance(e, ResponseCreate)]
    assert len(creates) >= 1
    tick = creates[0]
    assert tick.instructions == "idle prompt"
    assert tick.tools == [{"type": "function", "name": "nod"}]
    assert tick.output_modalities == ["text"]
    assert tick.tool_choice == "required"
    assert tick.metadata == {"palmimo_turn": "idle"}
