"""Tests for :mod:`palmimo_companion_agent.pipeline.speech` (fake mic/LLM/conductor; no hardware, no network).

:class:`SpeechPipeline` is driven end to end with a fake :class:`Subscription`
(a thread-safe queue, matching the real API's blocking ``get``), a real
:class:`~palmimo_companion_agent.pipeline.vad.UtteranceSegmenter` over a scripted fake
VAD (so utterance boundaries are deterministic), and fake ``llm`` /
``conductor`` collaborators -- no ONNX model, no ``sounddevice``, no network.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

import palmimo_companion_agent.pipeline.speech as speech_module
from palmimo_companion_agent.pipeline.llm import SpeechVerdict
from palmimo_companion_agent.pipeline.speech import SpeechPipeline
from palmimo_companion_agent.pipeline.vad import Int16Array, UtteranceSegmenter


FRAME = 512


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the subscription poll timeout so tests run in milliseconds."""
    monkeypatch.setattr(speech_module, "_SUBSCRIPTION_POLL_SECONDS", 0.02)


def _chunk(value: int = 100) -> Int16Array:
    return np.full(FRAME, value, dtype=np.int16)


class FakeVad:
    """Scripted VAD: pops queued probabilities per call; 0.0 (never starts) once exhausted."""

    def __init__(self, probs: list[float]) -> None:
        self._probs = list(probs)

    def __call__(self, frame: Int16Array) -> float:
        return self._probs.pop(0) if self._probs else 0.0

    def reset(self) -> None:
        pass


def _make_segmenter(probs: list[float]) -> UtteranceSegmenter:
    # silence_seconds=0.05 -> a 512-sample frame's worth of silence twice over
    # is enough to complete an utterance, so a handful of scripted frames
    # (rather than the real ~0.8s default) keeps these tests fast.
    return UtteranceSegmenter(vad=FakeVad(probs), sample_rate=16000, silence_seconds=0.05)


class FakeSubscription:
    """Thread-safe fake matching :class:`~palmimo_sdk.Subscription`'s blocking ``get``/``close``."""

    def __init__(self) -> None:
        self._queue: queue.Queue[Any] = queue.Queue()
        self.closed = False
        self.dropped = 0

    def push(self, chunk: Int16Array) -> None:
        self._queue.put(chunk)

    def get(self, timeout: float | None = None) -> Int16Array | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self.closed = True


class FakeMic:
    def __init__(self, sub: FakeSubscription) -> None:
        self._sub = sub

    def stream(self) -> FakeSubscription:
        return self._sub


class FakeLlm:
    """Queues of scripted transcribe/classify_speech results, popped in call order.

    Each queued item is either a plain value (returned) or an
    :class:`Exception` instance (raised) -- lets a test script a failure
    followed by a success on the same fake.
    """

    def __init__(self) -> None:
        self.transcribe_results: list[Any] = []
        self.classify_results: list[Any] = []
        self.transcribe_calls: list[bytes] = []
        self.classify_calls: list[str] = []

    async def transcribe(self, audio_bytes: bytes, *, language: str = "ja", filename: str = "audio.wav") -> str:
        self.transcribe_calls.append(audio_bytes)
        result = self.transcribe_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def classify_speech(self, text: str) -> SpeechVerdict:
        self.classify_calls.append(text)
        result = self.classify_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeConductor:
    def __init__(self) -> None:
        self.calls: list[tuple[SpeechVerdict, str]] = []

    def submit_speech(self, verdict: SpeechVerdict, text: str) -> None:
        self.calls.append((verdict, text))


async def _run_briefly(
    pipeline: SpeechPipeline,
    *,
    wait: float = 0.1,
    until: Callable[[], bool] | None = None,
    timeout: float = 2.0,
) -> None:
    """Run *pipeline* as a background task, then cancel it.

    With *until*, polls the predicate (up to *timeout* seconds) instead of
    sleeping a fixed *wait* -- fixed waits flake under a loaded full-suite
    run, where the pipeline's to_thread hops can take longer than the wait.
    Reaching *timeout* is not an error: the test's own assertions decide.
    """
    task = asyncio.create_task(pipeline.run())
    if until is None:
        await asyncio.sleep(wait)
    else:
        deadline = asyncio.get_running_loop().time() + timeout
        while not until() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_run_transcribes_classifies_and_submits_completed_utterance() -> None:
    sub = FakeSubscription()
    mic = FakeMic(sub)
    llm = FakeLlm()
    llm.transcribe_results = ["hello robot"]
    verdict = SpeechVerdict(kind="command", text="hello robot")
    llm.classify_results = [verdict]
    conductor = FakeConductor()
    segmenter = _make_segmenter([0.9, 0.0, 0.0])  # speech frame, then silence to completion

    pipeline = SpeechPipeline(mic=mic, llm=llm, conductor=conductor, segmenter=segmenter)
    for _ in range(3):
        sub.push(_chunk())

    await _run_briefly(pipeline, until=lambda: bool(conductor.calls))

    assert llm.transcribe_calls  # WAV bytes were sent
    assert llm.classify_calls == ["hello robot"]
    assert conductor.calls == [(verdict, "hello robot")]


async def test_run_logs_the_stt_transcription(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    sub = FakeSubscription()
    mic = FakeMic(sub)
    llm = FakeLlm()
    llm.transcribe_results = ["hello robot"]
    verdict = SpeechVerdict(kind="command", text="hello robot")
    llm.classify_results = [verdict]
    conductor = FakeConductor()
    segmenter = _make_segmenter([0.9, 0.0, 0.0])  # speech frame, then silence to completion

    pipeline = SpeechPipeline(mic=mic, llm=llm, conductor=conductor, segmenter=segmenter)
    for _ in range(3):
        sub.push(_chunk())

    await _run_briefly(pipeline, until=lambda: bool(conductor.calls))

    assert any("stt" in record.message and "hello robot" in record.message for record in caplog.records)


async def test_run_discards_empty_transcription_without_calling_guard() -> None:
    sub = FakeSubscription()
    mic = FakeMic(sub)
    llm = FakeLlm()
    llm.transcribe_results = ["   "]  # whitespace-only: treated as empty
    conductor = FakeConductor()
    segmenter = _make_segmenter([0.9, 0.0, 0.0])

    pipeline = SpeechPipeline(mic=mic, llm=llm, conductor=conductor, segmenter=segmenter)
    for _ in range(3):
        sub.push(_chunk())

    await _run_briefly(pipeline, until=lambda: bool(llm.transcribe_calls))

    assert llm.classify_calls == []
    assert conductor.calls == []


async def test_run_continues_after_transcribe_exception() -> None:
    sub = FakeSubscription()
    mic = FakeMic(sub)
    llm = FakeLlm()
    llm.transcribe_results = [RuntimeError("stt boom"), "second utterance"]
    verdict = SpeechVerdict(kind="command", text="second utterance")
    llm.classify_results = [verdict]
    conductor = FakeConductor()
    # Two utterances worth of frames: speech + silence, twice.
    segmenter = _make_segmenter([0.9, 0.0, 0.0, 0.9, 0.0, 0.0])

    pipeline = SpeechPipeline(mic=mic, llm=llm, conductor=conductor, segmenter=segmenter)
    for _ in range(6):
        sub.push(_chunk())

    await _run_briefly(pipeline, until=lambda: bool(conductor.calls))

    assert len(llm.transcribe_calls) == 2
    # The first (failed) utterance never reached the guard; only the second did.
    assert llm.classify_calls == ["second utterance"]
    assert conductor.calls == [(verdict, "second utterance")]


async def test_run_falls_back_to_command_on_classify_speech_exception() -> None:
    sub = FakeSubscription()
    mic = FakeMic(sub)
    llm = FakeLlm()
    llm.transcribe_results = ["first utterance", "second utterance"]
    verdict = SpeechVerdict(kind="ambient", text="second utterance")
    llm.classify_results = [RuntimeError("guard boom"), verdict]
    conductor = FakeConductor()
    segmenter = _make_segmenter([0.9, 0.0, 0.0, 0.9, 0.0, 0.0])

    pipeline = SpeechPipeline(mic=mic, llm=llm, conductor=conductor, segmenter=segmenter)
    for _ in range(6):
        sub.push(_chunk())

    await _run_briefly(pipeline, until=lambda: len(conductor.calls) >= 2)

    assert len(llm.transcribe_calls) == 2
    assert len(llm.classify_calls) == 2
    # The guard outage did not lose the first utterance: it fell back to a
    # conservative command verdict, and the second flowed through normally.
    assert len(conductor.calls) == 2
    fallback_verdict, fallback_text = conductor.calls[0]
    assert fallback_verdict.kind == "command"
    assert fallback_text == "first utterance"
    assert conductor.calls[1] == (verdict, "second utterance")


def test_pipeline_rejects_a_mic_at_the_wrong_sample_rate() -> None:
    sub = FakeSubscription()
    mic = FakeMic(sub)
    mic.sample_rate = 48000  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="48000"):
        SpeechPipeline(mic=mic, llm=FakeLlm(), conductor=FakeConductor(), segmenter=_make_segmenter([]))


async def test_run_exits_when_the_mic_stream_reports_closed() -> None:
    sub = FakeSubscription()
    mic = FakeMic(sub)
    mic.is_open = False  # type: ignore[attr-defined]
    pipeline = SpeechPipeline(mic=mic, llm=FakeLlm(), conductor=FakeConductor(), segmenter=_make_segmenter([]))

    async with asyncio.timeout(1.0):
        await pipeline.run()  # returns on its own -- no cancellation needed

    assert sub.closed


async def test_slow_transcription_does_not_stall_mic_reads() -> None:
    """While STT is in flight, the read loop must keep draining the subscription."""
    sub = FakeSubscription()
    mic = FakeMic(sub)
    llm = FakeLlm()
    release = asyncio.Event()

    async def slow_transcribe(audio_bytes: bytes, **kwargs: Any) -> str:
        await release.wait()
        return "slow text"

    llm.transcribe = slow_transcribe  # type: ignore[method-assign]
    conductor = FakeConductor()
    # Two utterances' worth of scripted probabilities.
    segmenter = _make_segmenter([0.9, 0.0, 0.0, 0.9, 0.0, 0.0])
    pipeline = SpeechPipeline(mic=mic, llm=llm, conductor=conductor, segmenter=segmenter)

    task = asyncio.ensure_future(pipeline.run())
    try:
        for _ in range(3):
            sub.push(_chunk())
        await asyncio.sleep(0.1)  # first utterance is now stuck in transcribe
        for _ in range(3):
            sub.push(_chunk())
        async with asyncio.timeout(1.0):
            while not sub._queue.empty():  # reads continue despite the stuck STT call
                await asyncio.sleep(0.01)
        release.set()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_run_closes_subscription_on_cancellation() -> None:
    sub = FakeSubscription()
    mic = FakeMic(sub)
    llm = FakeLlm()
    conductor = FakeConductor()
    segmenter = _make_segmenter([])  # nothing ever crosses the start threshold

    pipeline = SpeechPipeline(mic=mic, llm=llm, conductor=conductor, segmenter=segmenter)

    await _run_briefly(pipeline, wait=0.05)

    assert sub.closed is True


async def test_run_warns_when_subscription_dropped_counter_increases(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    sub = FakeSubscription()
    mic = FakeMic(sub)
    llm = FakeLlm()
    conductor = FakeConductor()
    segmenter = _make_segmenter([])  # nothing ever crosses the start threshold

    pipeline = SpeechPipeline(mic=mic, llm=llm, conductor=conductor, segmenter=segmenter)

    task = asyncio.create_task(pipeline.run())
    try:
        sub.push(_chunk())
        await asyncio.sleep(0.05)
        sub.dropped = 3  # simulate the drop-oldest buffer having discarded chunks
        sub.push(_chunk())
        deadline = asyncio.get_running_loop().time() + 2.0
        while not any("dropped" in record.message for record in caplog.records):
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    warnings = [record.message for record in caplog.records if "dropped" in record.message]
    assert any("3" in message for message in warnings)


async def test_run_ignores_a_poll_timeout_and_keeps_waiting() -> None:
    # No chunks pushed at all: every sub.get() call times out (returns None).
    # The loop must not raise or exit -- it just keeps polling until cancelled.
    sub = FakeSubscription()
    mic = FakeMic(sub)
    llm = FakeLlm()
    conductor = FakeConductor()
    segmenter = _make_segmenter([0.9, 0.0, 0.0])

    pipeline = SpeechPipeline(mic=mic, llm=llm, conductor=conductor, segmenter=segmenter)

    await _run_briefly(pipeline, wait=0.1)

    assert conductor.calls == []
    assert sub.closed is True
