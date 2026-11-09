"""Behavior of `palmimo_controller.ai.audio.Playback` with a recording stand-in for `aplay`."""

import threading
import time
from typing import Any

import pytest

from palmimo_controller.ai import audio
from palmimo_controller.ai.audio import API_RATE, Playback


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
