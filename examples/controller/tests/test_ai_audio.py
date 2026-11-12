"""Behavior of `palmimo_controller.ai.audio`: `Playback` with a recording stand-in for `aplay`,
and the gain and metering the mic feed and playback apply."""

import asyncio
import base64
import contextlib
import threading
import time
from typing import Any, cast

import numpy as np
import pytest

from palmimo_controller.ai import audio
from palmimo_controller.ai.audio import API_RATE, MicrophoneFeed, Playback
from palmimo_controller.ai.client import RealtimeClientLike


class _FakeStdin:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, pcm: bytes) -> None:
        if self.closed:
            raise BrokenPipeError
        self.data += pcm

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.stdin = _FakeStdin()
        self.killed = threading.Event()

    def poll(self) -> int | None:
        return 0 if self.killed.is_set() else None

    def kill(self) -> None:
        self.killed.set()

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[_FakeProcess]:
    processes: list[_FakeProcess] = []

    def popen(argv: list[str], **_kwargs: Any) -> _FakeProcess:
        process = _FakeProcess(argv)
        processes.append(process)
        return process

    monkeypatch.setattr(audio.subprocess, "Popen", popen)
    return processes


def _wait(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def test_written_audio_reaches_aplay_at_the_api_rate_on_the_chosen_device(spawned: list[_FakeProcess]) -> None:
    playback = Playback(device="plughw:CARD=Device_1,DEV=0")
    playback.write(b"\x01\x00" * 10)
    _wait(lambda: spawned and len(spawned[0].stdin.data) == 20)
    argv = spawned[0].argv
    assert argv[argv.index("-r") + 1] == str(API_RATE)
    assert argv[argv.index("-D") + 1] == "plughw:CARD=Device_1,DEV=0"
    playback.close()
    assert spawned[0].killed.is_set()


def test_speaking_lasts_as_long_as_the_written_audio() -> None:
    playback = Playback()
    assert not playback.speaking()
    # Never started, so no aplay: speaking is judged from what was written.
    playback._speaking_until = time.monotonic() + 0.5
    assert playback.speaking()
    assert not Playback().speaking(tail=0.0)


def test_write_extends_speaking_by_the_audio_duration(spawned: list[_FakeProcess]) -> None:
    playback = Playback()
    one_second = b"\x00\x00" * API_RATE
    before = time.monotonic()
    playback.write(one_second)
    assert playback._speaking_until >= before + 1.0
    assert playback.speaking()
    playback.close()


def test_interrupt_kills_playback_and_stops_speaking_at_once(spawned: list[_FakeProcess]) -> None:
    playback = Playback()
    playback.write(b"\x00\x00" * API_RATE)
    _wait(lambda: bool(spawned))
    playback.interrupt()
    assert not playback.speaking()
    _wait(spawned[0].killed.is_set)
    # The next reply starts a fresh aplay rather than writing to the dead one.
    playback.write(b"\x01\x00")
    _wait(lambda: len(spawned) == 2)
    playback.close()


def test_write_after_close_is_dropped(spawned: list[_FakeProcess]) -> None:
    playback = Playback()
    playback.close()
    playback.write(b"\x00\x00")
    time.sleep(0.05)
    assert spawned == []


def test_apply_gain_scales_and_clips_to_int16() -> None:
    pcm = np.array([1000, -1000, 20000, -20000], dtype=np.int16).tobytes()
    assert np.frombuffer(audio.apply_gain(pcm, 0.5), dtype=np.int16).tolist() == [500, -500, 10000, -10000]
    assert np.frombuffer(audio.apply_gain(pcm, 2.0), dtype=np.int16).tolist() == [2000, -2000, 32767, -32768]
    assert audio.apply_gain(pcm, 1.0) == pcm


def test_meter_level_spans_silence_to_full_scale() -> None:
    assert audio.meter_level(b"") == 0.0
    assert audio.meter_level(np.zeros(100, dtype=np.int16).tobytes()) == 0.0
    loud = audio.meter_level(np.full(100, 32767, dtype=np.int16).tobytes())
    quiet = audio.meter_level(np.full(100, 300, dtype=np.int16).tobytes())
    assert loud == pytest.approx(1.0, abs=0.01)
    assert 0.0 < quiet < loud


def test_playback_applies_the_current_volume_to_each_chunk(spawned: list[_FakeProcess]) -> None:
    # Without this, the homepage's speaker slider would not change how loud
    # the robot speaks, or would only apply to the next AI session.
    volume = [0.5]
    playback = Playback(volume=lambda: volume[0])
    playback.write(b"\x64\x00" * 2)  # 100
    volume[0] = 0.0
    playback.write(b"\x64\x00" * 2)
    _wait(lambda: spawned and len(spawned[0].stdin.data) == 8)
    assert bytes(spawned[0].stdin.data) == b"\x32\x00" * 2 + b"\x00\x00" * 2
    playback.close()


class _ChunkQueue:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = list(chunks)

    def get(self, timeout: float | None = None) -> Any:
        if self._chunks:
            return self._chunks.pop(0)
        time.sleep(0.01)
        return None


class _FakeMic:
    sample_rate = API_RATE

    def __init__(self, chunks: list[Any]) -> None:
        self._queue = _ChunkQueue(chunks)

    def stream(self) -> Any:
        return contextlib.nullcontext(self._queue)


class _RecordingClient:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)


class _RecordingLevel:
    def __init__(self) -> None:
        self.reports: list[float] = []
        self.cleared = False

    def report(self, level: float) -> None:
        self.reports.append(level)

    def clear(self) -> None:
        self.cleared = True


async def test_microphone_feed_applies_gain_and_meters_even_held_chunks() -> None:
    # Without this, the mic slider would not change what the model hears, and
    # the meter would freeze whenever the robot talks (chunks held back).
    chunks = [np.full(240, 1000, dtype=np.int16), np.full(240, 1000, dtype=np.int16)]
    client = _RecordingClient()
    level = _RecordingLevel()
    held = iter([False, True])
    feed = MicrophoneFeed(
        _FakeMic(chunks),
        cast(RealtimeClientLike, client),
        hold=lambda: next(held, True),
        gain=lambda: 2.0,
        level=level,
    )
    task = asyncio.create_task(feed.run())
    for _ in range(200):
        if len(level.reports) == 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(level.reports) == 2
    assert len(client.sent) == 1
    sent = np.frombuffer(base64.b64decode(client.sent[0].audio_b64), dtype=np.int16)
    assert sent.tolist() == [2000] * 240
    assert level.cleared
