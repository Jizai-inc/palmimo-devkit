"""Tests for :mod:`palmimo_wakeword_agent.main`'s ``listen_loop`` and its helper,
plus the entry point's output setup (:mod:`palmimo_wakeword_agent.output`).

Every dependency is a fake; no microphone, no VAD model, no LLM, no real
OpenAI calls.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import signal
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from palmimo_sdk.shutdown import STOP_SIGNALS, TERMINATING_SIGNALS, StopRequest, stop_flag_on_signals
from palmimo_wakeword_agent import main as main_module
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


# --- shutdown: the stop predicate and the signal handlers behind it ---


class EndlessSubscription(FakeSubscription):
    """A chunk feed that never ends on its own, capped so a broken stop check fails a test
    on the assertion instead of hanging the suite."""

    def __init__(self, cap: int = 1000) -> None:
        super().__init__([])
        self.cap = cap
        self.served = 0

    def __iter__(self) -> Iterator[str]:
        while self.served < self.cap:
            self.served += 1
            yield f"chunk-{self.served}"


def _idle_loop_kwargs(**overrides: Any) -> dict[str, Any]:
    """`listen_loop` keyword arguments whose utterances are never wired, so the loop only idles."""
    return {
        "segmenter": FakeSegmenter(set()),
        "detector": FakeDetector({}),
        "stt": lambda u: "",
        "agent": FakeAgent(),
        "say": Recorder().say,
        "is_speaking": _never_speaking,
    } | overrides


def _stop_on_call(n: int) -> Callable[[], bool]:
    """A stop predicate that stays ``False`` until its *n*-th call, then stays ``True``.

    Counting calls pins down *where* in the utterance handler the stop lands.
    """
    calls = 0

    def stop() -> bool:
        nonlocal calls
        calls += 1
        return calls >= n

    return stop


def _commanding_loop_kwargs(**overrides: Any) -> dict[str, Any]:
    """`listen_loop` keyword arguments where the first chunk is a full "<wake word>, <command>"."""
    return (
        _idle_loop_kwargs(
            segmenter=FakeSegmenter({"chunk-1"}),
            detector=FakeDetector({"パルミーモ前に進んで": WakeMatch(command="前に進んで")}),
            stt=lambda u: "パルミーモ前に進んで",
        )
        | overrides
    )


async def test_listen_loop_exits_when_stop_predicate_turns_true() -> None:
    chunks = EndlessSubscription()
    segmenter = FakeSegmenter(set())

    await listen_loop(chunks, **_idle_loop_kwargs(segmenter=segmenter, stop=_stop_on_call(4)))

    assert segmenter.fed == ["chunk-1", "chunk-2", "chunk-3"]
    assert chunks.served == 4  # the 4th chunk hit the stop check and never reached the segmenter


@pytest.mark.parametrize("signum", STOP_SIGNALS, ids=lambda s: s.name)
async def test_listen_loop_exits_on_signal_when_wrapped_in_the_shared_stop_flag(signum: signal.Signals) -> None:
    chunks = EndlessSubscription()
    delivered = 0

    def is_speaking() -> bool:
        nonlocal delivered
        delivered += 1
        if delivered == 3:
            signal.raise_signal(signum)
        return False

    with stop_flag_on_signals(StopRequest()) as stop:
        await listen_loop(chunks, **_idle_loop_kwargs(is_speaking=is_speaking, stop=stop))

    assert chunks.served == 4  # chunk 3 raised it; chunk 4's stop check ended the loop


async def test_listen_loop_cancels_an_in_flight_command_when_stopped() -> None:
    # AgentToolSet.call turns this CancelledError into a motion cancel, so the
    # robot stops moving instead of finishing the motion first.
    class SlowAgent:
        def __init__(self) -> None:
            self.cancelled = False
            self.finished = False

        async def run(self, text: str) -> str:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            self.finished = True
            return "進みます。"

    agent = SlowAgent()
    recorder = Recorder()

    await listen_loop(
        EndlessSubscription(),
        **_commanding_loop_kwargs(agent=agent, say=recorder.say, stop=_stop_on_call(3)),
    )

    assert agent.cancelled is True
    assert agent.finished is False
    assert recorder.said == []


async def test_listen_loop_does_not_start_a_command_when_stopped_during_stt() -> None:
    agent = FakeAgent()
    recorder = Recorder()

    await listen_loop(
        EndlessSubscription(),
        **_commanding_loop_kwargs(agent=agent, say=recorder.say, stop=_stop_on_call(2)),
    )

    assert agent.calls == []
    assert recorder.said == []


async def test_listen_loop_does_not_speak_a_reply_when_stopped_after_the_command() -> None:
    agent = FakeAgent(reply="進みます。")
    recorder = Recorder()

    await listen_loop(
        EndlessSubscription(),
        **_commanding_loop_kwargs(agent=agent, say=recorder.say, stop=_stop_on_call(3)),
    )

    assert agent.calls == ["前に進んで"]  # the command itself completed
    assert recorder.said == []


async def test_listen_loop_cancellation_reaches_the_in_flight_command() -> None:
    # The tool layer only stops the robot when its own coroutine is cancelled,
    # so cancelling the loop as a task must not orphan `agent.run`.
    class SlowAgent:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = False

        async def run(self, text: str) -> str:
            self.started.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return "進みます。"

    agent = SlowAgent()
    loop_task = asyncio.ensure_future(listen_loop(EndlessSubscription(), **_commanding_loop_kwargs(agent=agent)))
    await agent.started.wait()

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task

    assert agent.cancelled is True


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


# --- main(): every stop signal ends at the park ---


class _FakeStream:
    def __init__(self, chunks: EndlessSubscription) -> None:
        self._chunks = chunks

    def __enter__(self) -> EndlessSubscription:
        return self._chunks

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeMic:
    def __init__(self) -> None:
        self.chunks = EndlessSubscription()

    def stream(self) -> _FakeStream:
        return _FakeStream(self.chunks)


class _FakeRobot:
    def __init__(self) -> None:
        self.connected = False

    def connect(self) -> None:
        self.connected = True


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch, *, is_speaking: Callable[[], bool] = _never_speaking
) -> tuple[_FakeRobot, _FakeMic, list[bool]]:
    """Wire main() to fakes around the real listen loop, and record what the park was asked to do."""
    robot = _FakeRobot()
    mic = _FakeMic()
    parked: list[bool] = []
    runtime = SimpleNamespace(robot=robot, mic=mic, **_idle_loop_kwargs(is_speaking=is_speaking))
    monkeypatch.setattr(main_module, "build_runtime", lambda settings: runtime)
    monkeypatch.setattr(main_module, "park", lambda robot, *, legs=True: parked.append(legs))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    return robot, mic, parked


def _signal_on_call(n: int, *signums: signal.Signals) -> Callable[[], bool]:
    """An ``is_speaking`` that raises *signums* in order from its *n*-th call on, one per call."""
    calls = 0
    pending = list(signums)

    def is_speaking() -> bool:
        nonlocal calls
        calls += 1
        if calls >= n and pending:
            signal.raise_signal(pending.pop(0))
        return False

    return is_speaking


@pytest.mark.parametrize("signum", STOP_SIGNALS, ids=lambda s: s.name)
def test_main_parks_the_robot_when_a_signal_stops_the_listen_loop(
    monkeypatch: pytest.MonkeyPatch, signum: signal.Signals
) -> None:
    """The ssh-drop case: SIGHUP (and SIGTERM, SIGINT) mid-loop must reach the torque-off, legs first."""
    robot, mic, parked = _patch_runtime(monkeypatch, is_speaking=_signal_on_call(3, signum))

    result = CliRunner().invoke(main_module.app, [], catch_exceptions=False)

    assert robot.connected
    assert mic.chunks.served == 4  # the loop ended on its stop check, not on the cap
    assert parked == [True]
    assert "parking the robot" in result.output


@pytest.mark.parametrize("signum", STOP_SIGNALS, ids=lambda s: s.name)
def test_main_parks_the_robot_when_a_second_signal_interrupts_the_loop(
    monkeypatch: pytest.MonkeyPatch, signum: signal.Signals
) -> None:
    """A second signal is the operator saying the graceful path is too slow -- release torque, skip the leg return."""

    async def _wedged_loop(*args: object, stop: Callable[[], bool], **kwargs: object) -> None:
        # A loop that never reaches its stop check, like one blocked on a mic
        # that stopped delivering.
        signal.raise_signal(signum)
        assert stop()
        signal.raise_signal(signum)
        pytest.fail("the second signal did not interrupt the loop")

    _robot, _mic, parked = _patch_runtime(monkeypatch)
    monkeypatch.setattr(main_module, "listen_loop", _wedged_loop)

    CliRunner().invoke(main_module.app, [], catch_exceptions=False)

    assert parked == [False]


@pytest.mark.parametrize("signum", STOP_SIGNALS, ids=lambda s: s.name)
def test_main_parks_the_robot_when_a_signal_lands_during_connect(
    monkeypatch: pytest.MonkeyPatch, signum: signal.Signals
) -> None:
    """Torque is on from connect(), before the loop's own stop check exists.

    Without a handler there, SIGTERM/SIGHUP take the default disposition, which
    terminates the interpreter without unwinding -- nothing parks.
    """
    robot, _mic, parked = _patch_runtime(monkeypatch)

    def _signalled_connect() -> None:
        robot.connected = True
        signal.raise_signal(signum)

    monkeypatch.setattr(robot, "connect", _signalled_connect)

    CliRunner().invoke(main_module.app, [], catch_exceptions=False)

    assert parked == [False], "a signal during connect did not reach the park"


def test_main_hands_the_signal_dispositions_back_after_the_park(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leaked SIG_IGN would make the process unstoppable by SIGTERM/SIGHUP for the rest of its life."""
    _patch_runtime(monkeypatch, is_speaking=_signal_on_call(1, signal.SIGTERM))
    before = {signum: signal.getsignal(signum) for signum in STOP_SIGNALS}

    CliRunner().invoke(main_module.app, [], catch_exceptions=False)

    assert {signum: signal.getsignal(signum) for signum in STOP_SIGNALS} == before
    assert all(before[signum] is not signal.SIG_IGN for signum in TERMINATING_SIGNALS)
