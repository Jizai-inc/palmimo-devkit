"""Behavior of `palmimo_controller.ai.controller.AiController` against a fake Realtime client.

No socket, microphone, or speaker: the client, mic, and playback are
recorders, so a test can drive the server's event stream and see what the
controller sends back and what it does to the session.
"""

import asyncio
import base64
import contextlib
import json
import queue
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from palmimo_controller.ai import controller as controller_module
from palmimo_controller.ai.audio import API_RATE, to_api_rate
from palmimo_controller.ai.controller import AiController, OpenMic
from palmimo_controller.ai.protocol import (
    AudioAppend,
    ClientEvent,
    ItemCreate,
    ResponseCreate,
    SessionUpdate,
    parse_server_event,
)
from palmimo_controller.ai.tools import TOOL_DEFINITIONS
from palmimo_controller.modes import BundledPrompts, EditablePrompts, PromptSource
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
    def __init__(self, inbox: "queue.Queue[Any]") -> None:
        self._inbox = inbox

    def get(self, timeout: float) -> Any | None:
        try:
            return self._inbox.get(timeout=min(timeout, 0.01))
        except queue.Empty:
            return None

    def __enter__(self) -> "FakeChunks":
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class FakeMic:
    sample_rate = API_RATE

    def __init__(self) -> None:
        self.inbox: queue.Queue[Any] = queue.Queue()

    def stream(self) -> FakeChunks:
        return FakeChunks(self.inbox)


class FakePlayback:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.interrupts = 0
        self.closed = False
        self.talking = False

    def write(self, pcm: bytes) -> None:
        self.written.append(pcm)

    def interrupt(self) -> None:
        self.interrupts += 1

    def speaking(self, *, tail: float = 0.0) -> bool:
        return self.talking

    def close(self) -> None:
        self.closed = True


class Harness:
    def __init__(
        self,
        *,
        api_key: str | None = "sk-test",
        connect_error: BaseException | None = None,
        prompts: PromptSource | None = None,
        mic_error: BaseException | None = None,
        mic_gate: threading.Event | None = None,
        echo_cancelled: bool = True,
        voice: str = "coral",
        reflexes_factory: Any = None,
    ) -> None:
        self.session = PilotSession()
        self.session.acquire_pilot("a")
        self.client = FakeClient()
        self.mic = FakeMic()
        self.playback = FakePlayback()
        self.events: list[dict[str, Any]] = []
        self.connect_calls = 0
        self.mic_closed = False
        self._connect_error = connect_error
        self._mic_error = mic_error
        self._mic_gate = mic_gate
        self._echo_cancelled = echo_cancelled
        self.voice = voice
        self.ai = AiController(
            self.session,
            prompts if prompts is not None else BundledPrompts(),
            api_key=api_key,
            telemetry=Telemetry(emit=lambda line: self.events.append(json.loads(line))),
            mic_factory=self._open_mic,
            playback_factory=lambda: self.playback,
            connect=self._connect,
            voice=lambda: self.voice,
            start_timeout=1.0,
            reflexes_factory=reflexes_factory,
        )

    @contextlib.contextmanager
    def _open_mic(self) -> Iterator[OpenMic]:
        if self._mic_gate is not None:
            self._mic_gate.wait(2.0)
        if self._mic_error is not None:
            raise self._mic_error
        try:
            yield OpenMic(self.mic, echo_cancelled=self._echo_cancelled)
        finally:
            self.mic_closed = True

    def appended(self) -> list[AudioAppend]:
        return [event for event in self.client.sent if isinstance(event, AudioAppend)]

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


async def _live(h: Harness, mode: str = "default") -> None:
    assert await h.ai.start("a", mode)
    await _until(lambda: h.session.mode == MODE_AI)


async def test_start_goes_live_with_the_mode_prompt() -> None:
    h = Harness()
    assert await h.ai.start("a", "kansai")
    await _until(lambda: h.session.mode == MODE_AI)
    update = h.client.sent[0]
    assert isinstance(update, SessionUpdate)
    assert update.instructions == BundledPrompts().prompt("kansai")
    assert update.tools == TOOL_DEFINITIONS
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


async def test_tool_call_that_came_with_speech_does_not_ask_for_a_second_reply() -> None:
    # Without this, the robot would say "walking" and then talk again about having walked.
    h = Harness()
    await _live(h)
    h.client.push(
        {
            "type": "response.done",
            "response": {
                "output": [
                    {"type": "message", "role": "assistant", "content": []},
                    {"type": "function_call", "name": "stop", "arguments": "{}", "call_id": "c1"},
                ]
            },
        }
    )
    await _until(lambda: any(isinstance(event, ItemCreate) for event in h.client.sent))
    await asyncio.sleep(0.05)
    assert not any(isinstance(event, ResponseCreate) for event in h.client.sent)
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


async def test_start_uses_a_prompt_saved_after_the_app_started(tmp_path: Path) -> None:
    prompts = EditablePrompts(tmp_path)
    h = Harness(prompts=prompts)
    prompts.save("default", "edited prompt")
    await _live(h)
    update = h.client.sent[0]
    assert isinstance(update, SessionUpdate) and update.instructions == "edited prompt"
    await h.ai.stop()


async def test_mic_that_cannot_open_returns_to_teleop_without_connecting() -> None:
    h = Harness(mic_error=RuntimeError("no input device"))
    await h.ai.start("a", "default")
    await _until(lambda: h.session.mode == MODE_TELEOP)
    assert h.session.status().notice == controller_module.NOTICE_MIC
    assert h.connect_calls == 0


async def test_slow_mic_open_does_not_block_the_event_loop() -> None:
    # The same loop serves every pilot and viewer: a slow device open must not freeze them.
    gate = threading.Event()
    h = Harness(mic_gate=gate)
    await h.ai.start("a", "default")
    ticks = 0
    for _ in range(5):
        await asyncio.sleep(0.01)
        ticks += 1
    assert ticks == 5 and h.session.mode == MODE_AI_STARTING
    gate.set()
    await _until(lambda: h.session.mode == MODE_AI)
    await h.ai.stop()
    assert h.mic_closed


async def test_mic_audio_streams_to_the_model() -> None:
    h = Harness()
    await _live(h)
    h.mic.inbox.put(np.ones(240, dtype=np.int16))
    await _until(lambda: len(h.appended()) == 1)
    assert base64.b64decode(h.appended()[0].audio_b64) == np.ones(240, dtype=np.int16).tobytes()
    await h.ai.stop()


async def test_mic_without_echo_cancellation_is_held_while_the_robot_talks() -> None:
    # Without this, the booth robot would hear its own voice and answer itself.
    h = Harness(echo_cancelled=False)
    await _live(h)
    h.playback.talking = True
    h.mic.inbox.put(np.ones(240, dtype=np.int16))
    await asyncio.sleep(0.1)
    assert h.appended() == []
    h.playback.talking = False
    h.mic.inbox.put(np.ones(240, dtype=np.int16))
    await _until(lambda: len(h.appended()) == 1)
    await h.ai.stop()


@pytest.mark.parametrize(("rate", "samples"), [(16000, 1600), (24000, 2400), (48000, 4800)])
def test_mic_audio_reaches_the_api_at_24k_from_any_capture_rate(rate: int, samples: int) -> None:
    # Without this, the booth's 48 kHz USB mic would reach the model at half
    # speed, and nothing it hears would be understood.
    pcm = to_api_rate(np.zeros(samples, dtype=np.int16), rate)
    assert len(pcm) == 2 * API_RATE // 10


class _FakeReflexes:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def run(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_reflexes_run_while_ai_is_live_and_stop_with_it() -> None:
    reflexes = _FakeReflexes()
    h = Harness(reflexes_factory=lambda: reflexes)
    await _live(h)
    await asyncio.wait_for(reflexes.started.wait(), 2.0)
    await h.ai.stop()
    assert reflexes.cancelled


async def test_each_start_uses_the_voice_chosen_at_that_moment() -> None:
    h = Harness()
    await _live(h)
    first = h.client.sent[0]
    await h.ai.stop()
    h.voice = "marin"
    h.client = FakeClient()
    await _live(h)
    second = h.client.sent[0]
    assert isinstance(first, SessionUpdate) and isinstance(second, SessionUpdate)
    assert (first.voice, second.voice) == ("coral", "marin")
    await h.ai.stop()
