"""Tests for :mod:`palmimo_companion_agent.realtime.services.frames` -- the one-live-frame invariant."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from palmimo_companion_agent.realtime.log import EventLog
from palmimo_companion_agent.realtime.protocol import ItemCreate, ItemDelete
from palmimo_companion_agent.realtime.services.frames import FramePusher, LiveFrame
from palmimo_companion_agent.realtime.services.idle import IdlePacer
from palmimo_companion_agent.realtime.state import Sleeping


def _live_frame(jpeg: bytes | None = b"\xff\xd8jpeg") -> LiveFrame:
    frame = LiveFrame(camera=None)
    frame._encode = lambda: jpeg
    return frame


def _kinds(client: Any) -> list[str]:
    kinds = []
    for event in client.sent:
        if isinstance(event, ItemCreate):
            kinds.append(event.item.get("type") if event.item.get("type") != "message" else "create")
        elif isinstance(event, ItemDelete):
            kinds.append("delete")
    return kinds


async def test_pushing_a_frame_deletes_the_one_before_it(fake_client: type) -> None:
    """Realtime re-bills the whole context every turn -- exactly one frame stays live."""
    frame = _live_frame()
    client = fake_client()

    await frame.push(client)
    await frame.push(client)
    await frame.push(client)

    assert _kinds(client) == ["create", "delete", "create", "delete", "create"]
    deleted = [e.item_id for e in client.sent if isinstance(e, ItemDelete)]
    created = [e.item["id"] for e in client.sent if isinstance(e, ItemCreate)]
    assert deleted == created[:-1], "a frame was left live after being replaced"
    assert frame.pushed == 3


async def test_a_frame_the_camera_could_not_supply_sends_nothing(fake_client: type) -> None:
    frame = _live_frame(jpeg=None)
    client = fake_client()

    await frame.push(client)

    assert client.sent == []
    assert frame.pushed == 0


class _RecordingLog(EventLog):
    def __init__(self) -> None:
        super().__init__(None)
        self.entries: list[tuple[str, dict]] = []

    def write(self, kind: str, **fields: object) -> None:
        self.entries.append((kind, fields))


async def _run_briefly(coro: Any, seconds: float = 0.05) -> None:
    task = asyncio.ensure_future(coro)
    await asyncio.sleep(seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_frame_pusher_skips_pushing_while_busy(fake_client: type) -> None:
    client = fake_client()
    frame = _live_frame()
    pacer = IdlePacer()
    pacer.busy = True
    sleeping = Sleeping()
    pusher = FramePusher(client, frame, pacer, sleeping, 0.01, EventLog.open(None))

    await _run_briefly(pusher.run())

    assert frame.pushed == 0


async def test_frame_pusher_skips_pushing_while_asleep(fake_client: type) -> None:
    client = fake_client()
    frame = _live_frame()
    pacer = IdlePacer()
    sleeping = Sleeping()
    sleeping.asleep = True
    pusher = FramePusher(client, frame, pacer, sleeping, 0.01, EventLog.open(None))

    await _run_briefly(pusher.run())

    assert frame.pushed == 0


async def test_frame_pusher_pushes_a_frame_and_logs_it(fake_client: type) -> None:
    client = fake_client()
    frame = _live_frame()
    pacer = IdlePacer()
    sleeping = Sleeping()
    log = _RecordingLog()
    pusher = FramePusher(client, frame, pacer, sleeping, 0.01, log)

    await _run_briefly(pusher.run())

    assert frame.pushed >= 1
    assert ("frame_push", {"pushed": frame.pushed}) in log.entries
