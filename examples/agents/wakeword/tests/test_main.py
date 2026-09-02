"""Tests for :mod:`palmimo_wakeword_agent.main`'s ``listen_loop`` and its helper,
plus the entry point's output setup (:mod:`palmimo_wakeword_agent.output`).

Every dependency is a fake; no microphone, no VAD model, no LLM, no real
OpenAI calls.
"""

from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path

import pytest

from palmimo_wakeword_agent.main import _discard_stale_audio, _missing_api_key_error, listen_loop
from palmimo_wakeword_agent.output import configure_output
from palmimo_wakeword_agent.settings import WakewordAgentSettings
from palmimo_wakeword_agent.wakeword import WakeMatch


class FakeSubscription:
    """Fake standing in for a `palmimo_sdk.Subscription`-shaped audio feed.

    *chunks* is the fixed sequence `listen_loop` iterates over; *backlog* is
    what `get(timeout=0)` returns (in order) each time it's called, standing
    in for whatever piled up on a real mic queue while the loop was busy.
    """

    def __init__(self, chunks: list[str], backlog: list[str] | None = None, dropped: int = 0) -> None:
        self._chunks: Iterator[str] = iter(chunks)
        self._backlog = list(backlog or [])
        self.dropped = dropped

    def __iter__(self) -> Iterator[str]:
        return self._chunks

    def get(self, timeout: float | None = None) -> str | None:
        if self._backlog:
            return self._backlog.pop(0)
        return None


class FakeSegmenter:
    """Feeds back the chunk itself as the completed utterance for every chunk in *wired*."""

    def __init__(self, wired: set[str]) -> None:
        self._wired = wired
        self.reset_calls = 0
        self.fed: list[str] = []

    def feed(self, chunk: str) -> str | None:
        self.fed.append(chunk)
        return chunk if chunk in self._wired else None

    def reset(self) -> None:
        self.reset_calls += 1


class FakeDetector:
    def __init__(self, matches: dict[str, WakeMatch | None]) -> None:
        self._matches = matches

    def match(self, text: str) -> WakeMatch | None:
        return self._matches.get(text)


class FakeAgent:
    def __init__(self, reply: str = "了解しました。") -> None:
        self.reply = reply
        self.calls: list[str] = []

    async def run(self, text: str) -> str:
        self.calls.append(text)
        return self.reply


class Recorder:
    def __init__(self) -> None:
        self.said: list[str] = []

    def say(self, text: str) -> None:
        self.said.append(text)


def _never_speaking() -> bool:
    return False


async def test_wake_and_command_in_one_utterance_executes_and_speaks() -> None:
    chunks = FakeSubscription(["utt-1"])
    segmenter = FakeSegmenter({"utt-1"})
    detector = FakeDetector({"パルミーモ前に進んで": WakeMatch(command="前に進んで")})
    agent = FakeAgent(reply="進みます。")
    recorder = Recorder()

    await listen_loop(
        chunks,
        segmenter=segmenter,
        detector=detector,
        stt=lambda u: "パルミーモ前に進んで",
        agent=agent,
        say=recorder.say,
        is_speaking=_never_speaking,
    )

    assert agent.calls == ["前に進んで"]
    assert recorder.said == ["進みます。"]


async def test_non_wake_text_is_ignored_without_calling_agent() -> None:
    chunks = FakeSubscription(["utt-1"])
    segmenter = FakeSegmenter({"utt-1"})
    detector = FakeDetector({})  # no keys match -> None for anything
    agent = FakeAgent()
    recorder = Recorder()

    await listen_loop(
        chunks,
        segmenter=segmenter,
        detector=detector,
        stt=lambda u: "今日はいい天気ですね",
        agent=agent,
        say=recorder.say,
        is_speaking=_never_speaking,
    )

    assert agent.calls == []
    assert recorder.said == []


async def test_wake_word_alone_is_ignored() -> None:
    chunks = FakeSubscription(["utt-1"])
    segmenter = FakeSegmenter({"utt-1"})
    detector = FakeDetector({"パルミーモ": WakeMatch(command="")})
    agent = FakeAgent()
    recorder = Recorder()

    await listen_loop(
        chunks,
        segmenter=segmenter,
        detector=detector,
        stt=lambda u: "パルミーモ",
        agent=agent,
        say=recorder.say,
        is_speaking=_never_speaking,
    )

    assert agent.calls == []
    assert recorder.said == []


async def test_empty_stt_text_is_ignored() -> None:
    chunks = FakeSubscription(["utt-1"])
    segmenter = FakeSegmenter({"utt-1"})
    detector = FakeDetector({"": None})
    agent = FakeAgent()
    recorder = Recorder()

    await listen_loop(
        chunks,
        segmenter=segmenter,
        detector=detector,
        stt=lambda u: "",
        agent=agent,
        say=recorder.say,
        is_speaking=_never_speaking,
    )

    assert agent.calls == []
    assert recorder.said == []


async def test_stt_raising_prints_error_and_continues_to_next_utterance(capsys: pytest.CaptureFixture[str]) -> None:
    chunks = FakeSubscription(["utt-bad", "utt-good"])
    segmenter = FakeSegmenter({"utt-bad", "utt-good"})
    detector = FakeDetector({"パルミーモ前に進んで": WakeMatch(command="前に進んで")})
    agent = FakeAgent(reply="進みます。")
    recorder = Recorder()

    def stt(u: str) -> str:
        if u == "utt-bad":
            raise RuntimeError("transient stt failure")
        return "パルミーモ前に進んで"

    await listen_loop(
        chunks,
        segmenter=segmenter,
        detector=detector,
        stt=stt,
        agent=agent,
        say=recorder.say,
        is_speaking=_never_speaking,
    )

    assert agent.calls == ["前に進んで"]
    assert recorder.said == ["進みます。"]
    assert "[error]" in capsys.readouterr().out


async def test_agent_raising_prints_error_and_continues_to_next_utterance(capsys: pytest.CaptureFixture[str]) -> None:
    chunks = FakeSubscription(["utt-1", "utt-2"])
    segmenter = FakeSegmenter({"utt-1", "utt-2"})
    detector = FakeDetector({"パルミーモ前に進んで": WakeMatch(command="前に進んで")})

    # Raises only on its first call; the second, identical command succeeds.
    class TwoStageAgent:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self._raised = False

        async def run(self, text: str) -> str:
            self.calls.append(text)
            if not self._raised:
                self._raised = True
                raise RuntimeError("transient agent failure")
            return "進みます。"

    agent = TwoStageAgent()
    recorder = Recorder()

    await listen_loop(
        chunks,
        segmenter=segmenter,
        detector=detector,
        stt=lambda u: "パルミーモ前に進んで",
        agent=agent,
        say=recorder.say,
        is_speaking=_never_speaking,
    )

    assert agent.calls == ["前に進んで", "前に進んで"]
    assert recorder.said == ["進みます。"]
    assert "[error]" in capsys.readouterr().out


async def test_is_speaking_skips_chunk_and_resets_segmenter() -> None:
    chunks = FakeSubscription(["utt-1"])
    segmenter = FakeSegmenter({"utt-1"})
    detector = FakeDetector({})
    agent = FakeAgent()
    recorder = Recorder()
    stt_calls: list[str] = []

    def stt(u: str) -> str:
        stt_calls.append(u)
        return ""

    await listen_loop(
        chunks,
        segmenter=segmenter,
        detector=detector,
        stt=stt,
        agent=agent,
        say=recorder.say,
        is_speaking=lambda: True,
    )

    assert segmenter.fed == []  # feed() never called while speaking
    assert stt_calls == []  # stt never called while speaking
    assert segmenter.reset_calls == 1
    assert agent.calls == []


async def test_stale_audio_drained_and_reset_after_handling_an_utterance() -> None:
    chunks = FakeSubscription(["utt-1"], backlog=["stale-a", "stale-b"])
    segmenter = FakeSegmenter({"utt-1"})
    detector = FakeDetector({"パルミーモ前に進んで": WakeMatch(command="前に進んで")})
    agent = FakeAgent(reply="進みます。")
    recorder = Recorder()

    await listen_loop(
        chunks,
        segmenter=segmenter,
        detector=detector,
        stt=lambda u: "パルミーモ前に進んで",
        agent=agent,
        say=recorder.say,
        is_speaking=_never_speaking,
    )

    assert chunks._backlog == []  # drained
    assert segmenter.reset_calls == 1


async def test_dropped_warning_printed_when_dropped_grew_while_busy(capsys: pytest.CaptureFixture[str]) -> None:
    class GrowingDropSubscription(FakeSubscription):
        def get(self, timeout: float | None = None) -> str | None:
            self.dropped = 3
            return super().get(timeout)

    chunks = GrowingDropSubscription(["utt-1"])
    segmenter = FakeSegmenter({"utt-1"})
    detector = FakeDetector({"パルミーモ前に進んで": WakeMatch(command="前に進んで")})
    agent = FakeAgent(reply="進みます。")
    recorder = Recorder()

    await listen_loop(
        chunks,
        segmenter=segmenter,
        detector=detector,
        stt=lambda u: "パルミーモ前に進んで",
        agent=agent,
        say=recorder.say,
        is_speaking=_never_speaking,
    )

    out = capsys.readouterr().out
    assert "[warn]" in out
    assert "3 mic chunks dropped while busy" in out


# --- _discard_stale_audio, tested directly ---


def test_discard_stale_audio_drains_backlog_and_resets_segmenter() -> None:
    chunks = FakeSubscription([], backlog=["a", "b", "c"])
    segmenter = FakeSegmenter(set())

    _discard_stale_audio(chunks, segmenter)

    assert chunks.get() is None
    assert segmenter.reset_calls == 1


def test_discard_stale_audio_no_warning_when_dropped_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    chunks = FakeSubscription([], dropped=2)
    segmenter = FakeSegmenter(set())

    _discard_stale_audio(chunks, segmenter)

    assert "[warn]" not in capsys.readouterr().out


# --- _missing_api_key_error, tested directly ---


def _settings(**overrides: object) -> WakewordAgentSettings:
    return WakewordAgentSettings.with_env_file(None).model_copy(update=overrides)


def test_missing_api_key_error_flags_missing_openai_key() -> None:
    settings = _settings(openai_api_key=None, gemini_api_key="gk-1", command_model="gpt-5-nano")

    error = _missing_api_key_error(settings)

    assert error is not None
    assert "OPENAI_API_KEY" in error


def test_missing_api_key_error_flags_missing_gemini_key_for_default_model() -> None:
    settings = _settings(openai_api_key="sk-1", gemini_api_key=None, command_model="gemini-3.5-flash-lite")

    error = _missing_api_key_error(settings)

    assert error is not None
    assert "GEMINI_API_KEY" in error


def test_missing_api_key_error_none_when_gpt_model_lacks_gemini_key() -> None:
    settings = _settings(openai_api_key="sk-1", gemini_api_key=None, command_model="gpt-5-nano")

    assert _missing_api_key_error(settings) is None


def test_missing_api_key_error_none_when_all_required_keys_present() -> None:
    settings = _settings(openai_api_key="sk-1", gemini_api_key="gk-1", command_model="gemini-3.5-flash-lite")

    assert _missing_api_key_error(settings) is None


async def test_stale_audio_discarded_even_when_utterance_is_ignored() -> None:
    # Even an ignored utterance spent a network round-trip in stt() while
    # nobody read the mic queue, so the backlog is discarded (finally-path)
    # exactly like after a handled command.
    chunks = FakeSubscription(["utt-1"], backlog=["stale-a"])
    segmenter = FakeSegmenter({"utt-1"})
    agent = FakeAgent(reply="unused")
    recorder = Recorder()

    await listen_loop(
        chunks,
        segmenter=segmenter,
        detector=FakeDetector({}),  # never matches: the utterance is ignored
        stt=lambda u: "unrelated chatter",
        agent=agent,
        say=recorder.say,
        is_speaking=_never_speaking,
    )

    assert agent.calls == []
    assert chunks._backlog == []  # drained despite the ignore
    assert segmenter.reset_calls == 1


# --- configure_output, the entry point's output setup ---

#: Ceiling for every wait around the probe process, so a stdout that stays
#: block-buffered fails this test instead of hanging the whole suite.
PROBE_TIMEOUT_SECONDS = 20.0

#: Runs in a child process whose stdout is a pipe -- the exact condition
#: (stdout is not a TTY) under which Python block-buffers. It blocks on stdin
#: forever, so the parent can only read the printed line if it was flushed
#: while the process was still running.
LINE_BUFFERING_PROBE = """\
import sys

from palmimo_wakeword_agent.output import configure_output

configure_output()
assert sys.stdout.line_buffering is True
print("ready")
sys.stdin.read()
"""


def _first_line_before_exit(probe: str) -> str:
    """Run *probe* with stdout on a pipe and return its first line without waiting for exit."""
    # PYTHONUNBUFFERED would make the probe pass no matter what configure_output
    # does, so drop it from the child's environment.
    env = {name: value for name, value in os.environ.items() if name != "PYTHONUNBUFFERED"}
    process = subprocess.Popen(
        [sys.executable, "-c", probe],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdout is not None
    reader = ThreadPoolExecutor(max_workers=1)
    try:
        try:
            return reader.submit(process.stdout.readline).result(timeout=PROBE_TIMEOUT_SECONDS)
        except FuturesTimeoutError:
            pytest.fail(f"nothing readable within {PROBE_TIMEOUT_SECONDS}s: stdout is still block-buffered")
    finally:
        process.kill()  # the probe blocks on stdin by design; nothing waits for it to finish
        process.wait(timeout=PROBE_TIMEOUT_SECONDS)
        # Closing the pipe ends the pending readline(), so the reader thread is
        # already unblocked and this returns immediately.
        reader.shutdown(wait=False)


def test_configure_output_makes_a_printed_line_readable_before_the_process_exits() -> None:
    """The regression this guards: with stdout redirected, output stayed in the buffer and the agent looked dead."""
    assert _first_line_before_exit(LINE_BUFFERING_PROBE) == "ready\n"


def test_configure_output_does_not_raise_when_stdout_is_not_a_text_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pytest's own capture stream subclasses TextIOWrapper, so running under
    # capsys does NOT reach the guard. A substitute without reconfigure() does,
    # and is what a harness or an embedding host actually installs.
    class _NoReconfigure:
        def write(self, text: str) -> int:
            return len(text)

        def flush(self) -> None:
            return None

    monkeypatch.setattr(sys, "stdout", _NoReconfigure())
    sdk_logger = logging.getLogger("palmimo_sdk")
    original_level = sdk_logger.level
    try:
        configure_output()  # skips the line-buffering step rather than raising

        assert sdk_logger.level == logging.INFO  # the logging half still applied
    finally:
        sdk_logger.setLevel(original_level)


def test_every_console_script_configures_output() -> None:
    """An entry point that skips the setup is one whose output silently disappears under a redirect.

    Whichever console script the user happens to run has to do this, so the
    check reads them out of ``pyproject.toml`` rather than naming them here: a
    script added later is covered the moment it is declared.

    Reading ``co_names`` off the compiled function, rather than searching its
    source text, is what makes this a check on the call: a mention in a
    docstring or a commented-out line does not put the name there.
    """
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    scripts: dict[str, str] = pyproject["project"]["scripts"]
    assert scripts, "this project declares no console scripts"

    for script, target in scripts.items():
        module_name, _, function_name = target.partition(":")
        entry_point = getattr(importlib.import_module(module_name), function_name)
        assert "configure_output" in entry_point.__code__.co_names, (
            f"console script {script!r} ({target}) does not call configure_output()"
        )
