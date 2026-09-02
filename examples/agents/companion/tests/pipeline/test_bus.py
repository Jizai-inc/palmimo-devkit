"""Tests for :mod:`palmimo_companion_agent.pipeline.bus`."""

from __future__ import annotations

import asyncio

from palmimo_companion_agent.pipeline.bus import Bus, Interrupt


def test_interrupt_defaults_cancel_running_true() -> None:
    interrupt = Interrupt(kind="keyboard", text="hello")
    assert interrupt.cancel_running is True


def test_interrupt_defaults_directed_true() -> None:
    interrupt = Interrupt(kind="speech", text="hello")
    assert interrupt.directed is True


def test_interrupt_directed_false_is_carried_through_post_and_drain() -> None:
    bus = Bus()
    ambient = Interrupt(kind="speech", text="overheard chatter", cancel_running=False, directed=False)
    bus.post(ambient)
    [drained] = bus.drain()
    assert drained.directed is False


def test_post_then_drain_returns_posted_interrupts_in_order() -> None:
    bus = Bus()
    first = Interrupt(kind="keyboard", text="one")
    second = Interrupt(kind="speech", text="two", cancel_running=False)
    bus.post(first)
    bus.post(second)
    assert bus.drain() == [first, second]


def test_drain_empties_the_queue() -> None:
    bus = Bus()
    bus.post(Interrupt(kind="keyboard", text="note"))
    bus.drain()
    assert bus.drain() == []


def test_drain_on_empty_queue_returns_empty_list() -> None:
    bus = Bus()
    assert bus.drain() == []


def test_post_with_cancel_running_true_sets_cancel_event() -> None:
    bus = Bus()
    bus.post(Interrupt(kind="keyboard", text="stop", cancel_running=True))
    assert bus.cancel.is_set()


def test_post_with_cancel_running_false_leaves_cancel_event_clear() -> None:
    bus = Bus()
    bus.post(Interrupt(kind="speech", text="ambient chatter", cancel_running=False))
    assert not bus.cancel.is_set()


async def test_wait_returns_immediately_once_an_interrupt_is_posted() -> None:
    bus = Bus()
    bus.post(Interrupt(kind="keyboard", text="hi"))
    result = await bus.wait(1.0)
    assert result == [Interrupt(kind="keyboard", text="hi")]


async def test_wait_drains_any_interrupts_posted_after_the_first() -> None:
    bus = Bus()

    async def post_two_soon() -> None:
        await asyncio.sleep(0.01)
        bus.post(Interrupt(kind="keyboard", text="one"))
        bus.post(Interrupt(kind="keyboard", text="two"))

    task = asyncio.ensure_future(post_two_soon())
    result = await bus.wait(1.0)
    await task
    assert [itr.text for itr in result] == ["one", "two"]


async def test_wait_returns_empty_list_on_timeout() -> None:
    bus = Bus()
    result = await bus.wait(0.01)
    assert result == []


def test_drain_consumes_the_cancel_signal_atomically() -> None:
    bus = Bus()
    bus.post(Interrupt(kind="speech", text="stop it", cancel_running=True))
    assert bus.cancel.is_set()
    drained = bus.drain()
    assert len(drained) == 1
    assert not bus.cancel.is_set()


def test_drain_of_an_empty_queue_leaves_the_cancel_signal_alone() -> None:
    bus = Bus()
    bus.cancel.set()
    assert bus.drain() == []
    assert bus.cancel.is_set()


async def test_wait_consumes_the_cancel_signal_of_the_delivered_interrupt() -> None:
    bus = Bus()
    bus.post(Interrupt(kind="speech", text="stop it", cancel_running=True))
    delivered = await bus.wait(0.1)
    assert len(delivered) == 1
    assert not bus.cancel.is_set()
