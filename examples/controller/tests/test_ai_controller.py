"""Behavior of `palmimo_controller.ai.controller.AiController` against a fake Realtime client.

No socket, microphone, or speaker: the client, mic, and playback are
recorders, so a test can drive the server's event stream and see what the
controller sends back and what it does to the session.
"""

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from palmimo_controller.ai import controller as controller_module
from palmimo_controller.ai.controller import AiController
from palmimo_controller.ai.protocol import ClientEvent, ItemCreate, ResponseCreate, SessionUpdate, parse_server_event
from palmimo_controller.modes import BundledPrompts
from palmimo_controller.session import MODE_AI, MODE_AI_STARTING, MODE_TELEOP, PilotSession
from palmimo_controller.telemetry import Telemetry


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[ClientEvent] = []
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, event: ClientEvent) -> None:
        self.sent.append(event)

    async def events(self) -> AsyncIterator[Any]:
        while (raw := await self.inbox.get()) is not None:
            yield parse_server_event(raw)

    def push(self, payload: dict[str, Any]) -> None:
        self.inbox.put_nowait(json.dumps(payload))

    def close(self) -> None:
        self.inbox.put_nowait(None)


class FakeChunks:
    def get(self, timeout: float) -> None:
        time.sleep(min(timeout, 0.01))

    def __enter__(self) -> "FakeChunks":
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class FakeMic:
    def stream(self) -> FakeChunks:
        return FakeChunks()


class FakePlayback:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.interrupts = 0
        self.closed = False

    def write(self, pcm: bytes) -> None:
        self.written.append(pcm)

    def interrupt(self) -> None:
        self.interrupts += 1

    def close(self) -> None:
        self.closed = True


class Harness:
    def __init__(self, *, api_key: str | None = "sk-test", connect_error: BaseException | None = None) -> None:
        self.session = PilotSession()
        self.session.acquire_pilot("a")
        self.client = FakeClient()
        self.playback = FakePlayback()
        self.events: list[dict[str, Any]] = []
        self.connect_calls = 0
        self._connect_error = connect_error
        self.ai = AiController(
            self.session,
            BundledPrompts(),
            api_key=api_key,
            telemetry=Telemetry(emit=lambda line: self.events.append(json.loads(line))),
            mic_factory=self._mic,
            playback_factory=lambda: self.playback,
            connect=self._connect,
            start_timeout=1.0,
        )

    @contextlib.contextmanager
    def _mic(self) -> Iterator[FakeMic]:
        yield FakeMic()

    @contextlib.asynccontextmanager
    async def _connect(self, *, model: str, api_key: str) -> AsyncIterator[FakeClient]:
        self.connect_calls += 1
        if self._connect_error is not None:
            raise self._connect_error
        yield self.client

    def switches(self) -> list[dict[str, Any]]:
        return [event for event in self.events if event["type"] == "mode_switch"]


async def _until(predicate: Any, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_start_goes_live_with_the_mode_prompt() -> None:
    h = Harness()
    assert await h.ai.start("a", "kansai")
    await _until(lambda: h.session.mode == MODE_AI)
    update = h.client.sent[0]
    assert isinstance(update, SessionUpdate)
    assert update.instructions == BundledPrompts().prompt("kansai")
    switch = h.switches()[-1]
    assert (switch["from_mode"], switch["to_mode"], switch["ok"]) == ("teleop", "kansai", True)
    assert isinstance(switch["latency_ms"], int)
    await h.ai.stop()


async def test_start_without_a_key_stays_in_teleop_with_a_notice() -> None:
    # Without this, a missing key would leave the pilot stuck in "starting".
    h = Harness(api_key=None)
    assert await h.ai.start("a", "default") is False
    status = h.session.status()
    assert (status.mode, status.notice) == (MODE_TELEOP, controller_module.NOTICE_NO_KEY)
    assert h.connect_calls == 0


async def test_second_start_is_ignored_while_the_first_is_running() -> None:
    h = Harness()
    await h.ai.start("a", "default")
    assert await h.ai.start("a", "kansai") is False
    await _until(lambda: h.session.mode == MODE_AI)
    assert h.connect_calls == 1
    assert h.session.ai_mode == "default"
    await h.ai.stop()


class _RejectedError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.response = type("Response", (), {"status_code": status})()


@pytest.mark.parametrize(
    ("error", "notice"),
    [
        (_RejectedError(401), controller_module.NOTICE_BAD_KEY),
        (_RejectedError(429), controller_module.NOTICE_QUOTA),
        (OSError("no route to host"), controller_module.NOTICE_NETWORK),
    ],
)
async def test_failed_connect_returns_to_teleop_with_a_plain_notice(error: BaseException, notice: str) -> None:
    h = Harness(connect_error=error)
    await h.ai.start("a", "default")
    await _until(lambda: h.session.mode == MODE_TELEOP)
    assert h.session.status().notice == notice
    assert h.switches()[-1]["ok"] is False


async def test_slow_start_times_out_back_to_teleop() -> None:
    h = Harness()

    @contextlib.asynccontextmanager
    async def hang(*, model: str, api_key: str) -> AsyncIterator[FakeClient]:
        await asyncio.Event().wait()
        yield h.client

    h.ai._connect = hang
    h.ai._start_timeout = 0.05
    await h.ai.start("a", "default")
    assert h.session.mode == MODE_AI_STARTING
    await _until(lambda: h.session.mode == MODE_TELEOP)
    assert h.session.status().notice == controller_module.NOTICE_TIMEOUT


async def test_socket_closing_mid_session_returns_to_teleop() -> None:
    # Without this, a dropped network would leave the robot in AI mode, silent.
    h = Harness()
    await h.ai.start("a", "default")
    await _until(lambda: h.session.mode == MODE_AI)
    h.client.close()
    await _until(lambda: h.session.mode == MODE_TELEOP)
    assert h.session.status().notice == controller_module.NOTICE_NETWORK
    assert h.playback.closed
    assert any(event["type"] == "ai_error" for event in h.events)


async def test_quota_error_event_ends_the_session() -> None:
    h = Harness()
    await h.ai.start("a", "default")
    await _until(lambda: h.session.mode == MODE_AI)
    h.client.push({"type": "error", "error": {"code": "insufficient_quota"}})
    await _until(lambda: h.session.mode == MODE_TELEOP)
    assert h.session.status().notice == controller_module.NOTICE_QUOTA


async def test_tool_call_moves_the_robot_and_answers_the_model() -> None:
    h = Harness()
    await h.ai.start("a", "default")
    await _until(lambda: h.session.mode == MODE_AI)
    h.client.push(
        {
            "type": "response.done",
            "response": {
                "output": [
                    {
                        "type": "function_call",
                        "name": "walk",
                        "arguments": '{"direction": "forward", "seconds": 2}',
                        "call_id": "c1",
                    }
                ]
            },
        }
    )
    await _until(lambda: any(isinstance(event, ResponseCreate) for event in h.client.sent))
    assert h.session.effective_input().move == "forward"
    outputs = [event for event in h.client.sent if isinstance(event, ItemCreate)]
    assert outputs[0].item == {"type": "function_call_output", "call_id": "c1", "output": '{"ok": true}'}
    await h.ai.stop()


async def test_audio_plays_and_barge_in_silences_it() -> None:
    h = Harness()
    await h.ai.start("a", "default")
    await _until(lambda: h.session.mode == MODE_AI)
    h.client.push({"type": "response.output_audio.delta", "delta": "AAAA"})
    h.client.push({"type": "input_audio_buffer.speech_started"})
    await _until(lambda: h.playback.interrupts == 1)
    assert h.playback.written == [b"\x00\x00\x00"]
    await h.ai.stop()


async def test_stop_returns_to_teleop_and_silences_the_robot() -> None:
    h = Harness()
    await h.ai.start("a", "default")
    await _until(lambda: h.session.mode == MODE_AI)
    await h.ai.stop()
    assert h.session.mode == MODE_TELEOP
    assert h.session.status().notice is None
    assert h.playback.interrupts >= 1 and h.playback.closed
    assert not h.ai.running
    assert h.switches()[-1]["to_mode"] == "teleop"


async def test_stop_during_start_cancels_the_switch() -> None:
    h = Harness()

    @contextlib.asynccontextmanager
    async def hang(*, model: str, api_key: str) -> AsyncIterator[FakeClient]:
        await asyncio.Event().wait()
        yield h.client

    h.ai._connect = hang
    await h.ai.start("a", "default")
    await h.ai.stop()
    assert h.session.mode == MODE_TELEOP
    assert not h.ai.running


async def test_stop_is_harmless_in_teleop() -> None:
    h = Harness()
    await h.ai.stop()
    assert h.session.mode == MODE_TELEOP
    assert h.switches() == []
