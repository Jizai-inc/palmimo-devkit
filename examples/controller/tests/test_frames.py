"""Behavior of `palmimo_controller.ai.frames`: one live frame in the conversation, sent while the robot is quiet."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from palmimo_controller.ai.frames import FramePusher, LiveFrame
from palmimo_controller.ai.protocol import ClientEvent, ItemCreate, ItemDelete
from palmimo_controller.telemetry import Telemetry


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[ClientEvent] = []

    async def send(self, event: ClientEvent) -> None:
        self.sent.append(event)

    def events(self) -> AsyncIterator[Any]:  # pragma: no cover - not used by frames
        raise NotImplementedError


def _images(client: FakeClient) -> list[ItemCreate]:
    return [event for event in client.sent if isinstance(event, ItemCreate)]


async def test_each_frame_replaces_the_one_before() -> None:
    # Without this, every old frame would stay in context and be re-billed each turn.
    client = FakeClient()
    frame = LiveFrame(lambda: b"jpeg")
    assert await frame.push(client)
    assert await frame.push(client)
    first, delete, second = client.sent
    assert isinstance(first, ItemCreate) and isinstance(second, ItemCreate)
    assert delete == ItemDelete(item_id=first.item["id"])
    assert first.item["id"] != second.item["id"]
    assert first.item["content"][0]["type"] == "input_image"


async def test_no_frame_ready_sends_nothing() -> None:
    client = FakeClient()
    assert not await LiveFrame(lambda: None).push(client)
    assert client.sent == []


async def _run_for(pusher: FramePusher, seconds: float) -> None:
    task = asyncio.create_task(pusher.run())
    await asyncio.sleep(seconds)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_frames_are_sent_on_a_cadence() -> None:
    client = FakeClient()
    events: list[dict[str, Any]] = []
    pusher = FramePusher(
        LiveFrame(lambda: b"jpeg"),
        client,
        busy=lambda: False,
        telemetry=Telemetry(emit=lambda line: events.append(json.loads(line))),
        interval=0.02,
    )
    await _run_for(pusher, 0.09)
    assert len(_images(client)) >= 3
    assert [event["reason"] for event in events][:2] == ["start", "periodic"]


async def test_no_frame_is_sent_while_the_robot_is_busy() -> None:
    # A frame mid-reply would land in the middle of what the model is saying.
    client = FakeClient()
    busy = True
    pusher = FramePusher(LiveFrame(lambda: b"jpeg"), client, busy=lambda: busy, interval=0.01, busy_retry=0.005)
    await _run_for(pusher, 0.05)
    assert _images(client) == []
    busy = False
    await _run_for(pusher, 0.02)
    assert _images(client)


async def test_a_nudge_sends_a_frame_without_waiting_for_the_cadence() -> None:
    client = FakeClient()
    events: list[dict[str, Any]] = []
    pusher = FramePusher(
        LiveFrame(lambda: b"jpeg"),
        client,
        busy=lambda: False,
        telemetry=Telemetry(emit=lambda line: events.append(json.loads(line))),
        interval=60.0,
    )
    task = asyncio.create_task(pusher.run())
    await asyncio.sleep(0.01)
    pusher.nudge()
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert [event["reason"] for event in events] == ["start", "face"]


async def test_a_nudge_while_busy_sends_exactly_one_frame_once_quiet() -> None:
    # A periodic frame and a face frame used to go out a few milliseconds apart.
    client = FakeClient()
    events: list[dict[str, Any]] = []
    busy = True
    pusher = FramePusher(
        LiveFrame(lambda: b"jpeg"),
        client,
        busy=lambda: busy,
        telemetry=Telemetry(emit=lambda line: events.append(json.loads(line))),
        interval=60.0,
        busy_retry=0.005,
    )
    task = asyncio.create_task(pusher.run())
    await asyncio.sleep(0.01)
    pusher.nudge()
    await asyncio.sleep(0.01)
    busy = False
    await asyncio.sleep(0.03)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert [event["reason"] for event in events] == ["face"]
