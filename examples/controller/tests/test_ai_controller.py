"""Behavior of `palmimo_controller.ai.controller.AiController` with a scripted LLM.

No network, microphone, speaker, or VAD model: the LLM replies from a script,
the mic yields one chunk per utterance the test queues, and the segmenter
turns each chunk straight into an utterance.
"""

import asyncio
import contextlib
import json
import queue
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from palmimo_controller.ai.audio import to_pipeline_rate
from palmimo_controller.ai.controller import (
    NOTICE_BAD_KEY,
    NOTICE_MIC,
    NOTICE_NETWORK,
    NOTICE_NO_KEY,
    NOTICE_QUOTA,
    NOTICE_TIMEOUT,
    NOTICE_VOICE,
    AiController,
    OpenMic,
    OpenVoice,
)
from palmimo_controller.ai.llm import ChatReply, LlmError, ToolCall
from palmimo_controller.ai.vad import SAMPLE_RATE
from palmimo_controller.modes import BundledPrompts, EditablePrompts, PromptSource
from palmimo_controller.session import MODE_AI, MODE_AI_STARTING, MODE_TELEOP, PilotSession
from palmimo_controller.telemetry import Telemetry


UTTERANCE = np.zeros(1600, dtype=np.int16)


class FakeLlm:
    def __init__(self, *, check_error: BaseException | None = None, check_delay: float = 0.0) -> None:
        self.check_error = check_error
        self.check_delay = check_delay
        self.transcripts: list[str | BaseException] = []
        self.replies: list[ChatReply] = []
        self.chats: list[list[dict[str, Any]]] = []

    async def check(self) -> None:
        await asyncio.sleep(self.check_delay)
        if self.check_error is not None:
            raise self.check_error

    async def transcribe(self, wav: bytes) -> str:
        item = self.transcripts.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def chat(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None) -> ChatReply:
        self.chats.append(list(messages))
        return self.replies.pop(0) if self.replies else ChatReply(text="")


class FakeChunks:
    def __init__(self, inbox: "queue.Queue[Any]") -> None:
        self._inbox = inbox

    def get(self, timeout: float) -> Any | None:
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            return None

    def __enter__(self) -> "FakeChunks":
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class FakeMic:
    sample_rate = SAMPLE_RATE

    def __init__(self) -> None:
        self.inbox: queue.Queue[Any] = queue.Queue()

    def stream(self) -> FakeChunks:
        return FakeChunks(self.inbox)


class FakeSegmenter:
    """Every chunk is a whole utterance."""

    speaking = False

    def feed(self, chunk: Any) -> Any:
        return chunk

    def reset(self) -> None:
        pass


class FakeHandle:
    def is_alive(self) -> bool:
        return False


class FakeSpeaker:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.stops = 0

    def say(self, text: str) -> FakeHandle:
        self.said.append(text)
        return FakeHandle()

    def stop(self) -> None:
        self.stops += 1

    def close(self) -> None:
        pass


class Harness:
    def __init__(
        self,
        *,
        llm: FakeLlm | None = None,
        no_key: bool = False,
        prompts: PromptSource | None = None,
        mic_error: BaseException | None = None,
        mic_gate: threading.Event | None = None,
        voice_error: BaseException | None = None,
        reflexes_factory: Any = None,
    ) -> None:
        self.session = PilotSession()
        self.session.acquire_pilot("a")
        self.llm = llm or FakeLlm()
        self.mic = FakeMic()
        self.speaker = FakeSpeaker()
        self.events: list[dict[str, Any]] = []
        self.mic_closed = False
        self._mic_error = mic_error
        self._mic_gate = mic_gate
        self._voice_error = voice_error
        self.ai = AiController(
            self.session,
            prompts if prompts is not None else BundledPrompts(),
            llm=None if no_key else self.llm,
            telemetry=Telemetry(emit=lambda line: self.events.append(json.loads(line))),
            mic_factory=self._open_mic,
            voice_factory=self._open_voice,
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
            yield OpenMic(self.mic, echo_cancelled=True)
        finally:
            self.mic_closed = True

    @contextlib.contextmanager
    def _open_voice(self) -> Iterator[OpenVoice]:
        if self._voice_error is not None:
            raise self._voice_error
        yield OpenVoice(self.speaker, FakeSegmenter())

    def hear(self, transcript: str | BaseException) -> None:
        self.llm.transcripts.append(transcript)
        self.mic.inbox.put(UTTERANCE)

    def switches(self) -> list[dict[str, Any]]:
        return [event for event in self.events if event["type"] == "mode_switch"]


async def _until(predicate: Any, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def _live(h: Harness, mode: str = "default") -> None:
    assert await h.ai.start("a", mode)
    await _until(lambda: h.session.mode == MODE_AI)


async def test_start_goes_live_and_answers_with_the_mode_prompt() -> None:
    h = Harness()
    h.llm.replies.append(ChatReply(text="こんにちは"))
    await _live(h, "kansai")
    switch = h.switches()[-1]
    assert (switch["from_mode"], switch["to_mode"], switch["ok"]) == ("teleop", "kansai", True)
    assert isinstance(switch["latency_ms"], int)
    h.hear("こんにちは、元気？")
    await _until(lambda: h.speaker.said == ["こんにちは"])
    assert h.llm.chats[0][0] == {"role": "system", "content": BundledPrompts().prompt("kansai")}
    await h.ai.stop()


async def test_start_uses_a_prompt_saved_after_the_app_started(tmp_path: Path) -> None:
    prompts = EditablePrompts(tmp_path)
    h = Harness(prompts=prompts)
    prompts.save("default", "edited prompt")
    await _live(h)
    h.hear("もしもし")
    await _until(lambda: len(h.llm.chats) == 1)
    assert h.llm.chats[0][0]["content"] == "edited prompt"
    await h.ai.stop()


async def test_start_without_a_key_stays_in_teleop_with_a_notice() -> None:
    h = Harness(no_key=True)
    assert await h.ai.start("a", "default") is False
    assert h.session.mode == MODE_TELEOP
    assert h.session.status().notice == NOTICE_NO_KEY
    assert not h.ai.available


async def test_second_start_is_ignored_while_the_first_is_running() -> None:
    h = Harness(llm=FakeLlm(check_delay=0.2))
    assert await h.ai.start("a", "default")
    assert await h.ai.start("a", "kansai") is False
    assert h.session.ai_mode == "default"
    await h.ai.stop()


@pytest.mark.parametrize(
    ("error", "notice"),
    [
        (LlmError("auth", "AuthenticationError"), NOTICE_BAD_KEY),
        (LlmError("quota", "insufficient_quota"), NOTICE_QUOTA),
        (LlmError("transient", "network"), NOTICE_NETWORK),
    ],
)
async def test_failed_api_check_returns_to_teleop_with_a_plain_notice(error: BaseException, notice: str) -> None:
    h = Harness(llm=FakeLlm(check_error=error))
    await h.ai.start("a", "default")
    await _until(lambda: h.session.mode == MODE_TELEOP)
    assert h.session.status().notice == notice
    assert h.switches()[-1]["ok"] is False
    assert h.mic_closed


async def test_slow_start_times_out_back_to_teleop() -> None:
    h = Harness(llm=FakeLlm(check_delay=5.0))
    await h.ai.start("a", "default")
    assert h.session.mode == MODE_AI_STARTING
    await _until(lambda: h.session.mode == MODE_TELEOP, timeout=3.0)
    assert h.session.status().notice == NOTICE_TIMEOUT


async def test_mic_that_cannot_open_returns_to_teleop() -> None:
    h = Harness(mic_error=RuntimeError("no input device"))
    await h.ai.start("a", "default")
    await _until(lambda: h.session.mode == MODE_TELEOP)
    assert h.session.status().notice == NOTICE_MIC


async def test_slow_mic_open_does_not_block_the_event_loop() -> None:
    # The same loop serves every pilot and viewer: a first-run model download
    # inside the mic open must not freeze them.
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


async def test_voice_that_cannot_load_returns_to_teleop_and_closes_the_mic() -> None:
    h = Harness(voice_error=RuntimeError("piper voice missing"))
    await h.ai.start("a", "default")
    await _until(lambda: h.session.mode == MODE_TELEOP)
    assert h.session.status().notice == NOTICE_VOICE
    assert h.mic_closed


async def test_tool_call_moves_the_robot_and_the_result_stays_on_record() -> None:
    h = Harness()
    h.llm.replies += [
        ChatReply(text="", tool_calls=[ToolCall("c1", "walk", '{"direction": "forward", "seconds": 2}')]),
        ChatReply(text="歩きます"),
    ]
    await _live(h)
    h.hear("前に歩いて")
    await _until(lambda: h.speaker.said == ["歩きます"])
    assert h.session.effective_input().move == "forward"
    tool_results = [m for m in h.llm.chats[-1] if m.get("role") == "tool"]
    assert tool_results and json.loads(tool_results[0]["content"])["ok"] is True
    await h.ai.stop()


async def test_noise_is_not_sent_to_the_model() -> None:
    h = Harness()
    await _live(h)
    h.hear("")
    h.hear("もしもし")
    await _until(lambda: len(h.llm.chats) == 1)
    assert h.llm.chats[0][-1] == {"role": "user", "content": "もしもし"}
    await h.ai.stop()


async def test_one_network_failure_drops_the_turn_but_keeps_listening() -> None:
    h = Harness()
    h.llm.replies.append(ChatReply(text="はい"))
    await _live(h)
    h.hear(LlmError("transient", "network"))
    h.hear("もしもし")
    await _until(lambda: h.speaker.said == ["はい"])
    assert h.session.mode == MODE_AI
    await h.ai.stop()


async def test_a_rejected_key_mid_mode_returns_to_teleop() -> None:
    h = Harness()
    await _live(h)
    h.hear(LlmError("auth", "AuthenticationError"))
    await _until(lambda: h.session.mode == MODE_TELEOP)
    assert h.session.status().notice == NOTICE_BAD_KEY


async def test_stop_returns_to_teleop_and_silences_the_robot() -> None:
    h = Harness()
    await _live(h)
    await h.ai.stop()
    assert h.session.mode == MODE_TELEOP
    assert h.session.status().notice is None
    assert h.speaker.stops >= 1 and h.mic_closed
    assert not h.ai.running
    assert h.switches()[-1]["to_mode"] == "teleop"


async def test_stop_during_start_cancels_the_switch() -> None:
    h = Harness(llm=FakeLlm(check_delay=5.0))
    await h.ai.start("a", "default")
    await h.ai.stop()
    assert h.session.mode == MODE_TELEOP
    assert not h.ai.running


async def test_stop_is_harmless_in_teleop() -> None:
    h = Harness()
    await h.ai.stop()
    assert h.session.mode == MODE_TELEOP
    assert h.switches() == []


@pytest.mark.parametrize(("rate", "samples"), [(16000, 1600), (48000, 4800)])
def test_mic_audio_reaches_the_pipeline_at_16k_from_any_capture_rate(rate: int, samples: int) -> None:
    # Without this, the booth's 48 kHz USB mic would reach the VAD and the
    # transcriber at triple speed, and nothing it hears would be understood.
    pcm = to_pipeline_rate(np.zeros(samples, dtype=np.int16), rate)
    assert pcm.dtype == np.int16 and len(pcm) == SAMPLE_RATE // 10


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
