"""Tests for :mod:`palmimo_companion_agent.pipeline.ui.cli` -- the headless front end.

:func:`build_runtime` is monkeypatched to a hand-assembled :class:`Runtime`
over a compute-only :class:`~palmimo_sdk.Palmimo` and a fake LLM (no network,
no hardware); stdin is a plain in-memory stream.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import time
from typing import ClassVar

import pytest

import palmimo_companion_agent.pipeline.ui.cli as cli_module
from palmimo_companion_agent.core.tools import COMPANION_TOOL_MODELS
from palmimo_companion_agent.pipeline.bus import Bus
from palmimo_companion_agent.pipeline.conductor import Conductor
from palmimo_companion_agent.pipeline.history import History
from palmimo_companion_agent.pipeline.llm import SpeechVerdict
from palmimo_companion_agent.pipeline.settings import PipelineSettings
from palmimo_companion_agent.pipeline.wiring import Runtime
from palmimo_sdk import Palmimo
from palmimo_sdk.agent.toolset import AgentToolSet


class _FakeMessage:
    """A tool-free assistant turn: the conductor idles rather than looping."""

    content: str | None = None
    tool_calls: None = None


class _FakeChoice:
    message = _FakeMessage()


class _FakeResponse:
    choices: ClassVar[list[_FakeChoice]] = [_FakeChoice()]


class FakeLlm:
    """Stands in for :class:`~palmimo_companion_agent.pipeline.llm.LlmProvider`: never calls a real model."""

    async def chat(self, messages: list[dict], *, tools: list[dict] | None = None, **_: object) -> _FakeResponse:
        return _FakeResponse()

    async def classify_speech(self, text: str) -> SpeechVerdict:
        return SpeechVerdict(kind="command", text=text)


def _toolset() -> AgentToolSet:
    ts = AgentToolSet(Palmimo(), exclude=["creep", "wave"])
    for cls in COMPANION_TOOL_MODELS.values():
        ts.register(cls)
    return ts


def _build_test_runtime() -> Runtime:
    history = History()
    bus = Bus()
    palmimo = Palmimo()
    conductor = Conductor(history, _toolset(), FakeLlm(), bus, event_log=None)
    return Runtime(conductor=conductor, palmimo=palmimo, toolset=_toolset(), history=history, event_log=None)


def _settings() -> PipelineSettings:
    return PipelineSettings.with_env_file(None).model_copy(update={"hardware": False})


async def test_run_cli_emits_jsonl_events_and_exits_on_slash_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_module, "build_runtime", lambda settings: _build_test_runtime())
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello there\n/exit\n"))

    await asyncio.wait_for(cli_module.run_cli(_settings()), timeout=5.0)

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]
    kinds = [event["kind"] for event in events]

    assert "system_note" in kinds  # the startup note
    assert "keyboard" in kinds
    keyboard_events = [event for event in events if event["kind"] == "keyboard"]
    assert keyboard_events[0]["text"] == "hello there"


async def test_run_cli_ignores_blank_lines(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli_module, "build_runtime", lambda settings: _build_test_runtime())
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n   \n/exit\n"))

    await asyncio.wait_for(cli_module.run_cli(_settings()), timeout=5.0)

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]
    assert all(event["kind"] != "keyboard" for event in events)


async def test_run_cli_hear_command_submits_as_speech(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_module, "build_runtime", lambda settings: _build_test_runtime())
    monkeypatch.setattr(sys, "stdin", io.StringIO("/hear hello there\n/exit\n"))

    await asyncio.wait_for(cli_module.run_cli(_settings()), timeout=5.0)

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]
    mic_events = [event for event in events if event["kind"] == "mic_input"]
    assert mic_events
    assert mic_events[0]["text"] == "hello there"
    assert all(event["kind"] != "keyboard" for event in events)


async def test_run_cli_hear_with_no_text_warns_and_is_ignored(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_module, "build_runtime", lambda settings: _build_test_runtime())
    monkeypatch.setattr(sys, "stdin", io.StringIO("/hear\n/hear   \n/exit\n"))

    await asyncio.wait_for(cli_module.run_cli(_settings()), timeout=5.0)

    out = capsys.readouterr()
    lines = [line for line in out.out.splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]
    assert all(event["kind"] != "mic_input" for event in events)
    assert "/hear" in out.err


async def test_run_cli_exits_on_eof_without_slash_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_module, "build_runtime", lambda settings: _build_test_runtime())
    monkeypatch.setattr(sys, "stdin", io.StringIO("hi\n"))  # no /exit -- EOF ends the loop

    await asyncio.wait_for(cli_module.run_cli(_settings()), timeout=5.0)

    out = capsys.readouterr().out
    assert '"text": "hi"' in out


async def test_run_cli_closes_the_event_log_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    log = io.StringIO()

    def _fake_build_runtime(settings: PipelineSettings) -> Runtime:
        rt = _build_test_runtime()
        rt.event_log = log
        return rt

    monkeypatch.setattr(cli_module, "build_runtime", _fake_build_runtime)
    monkeypatch.setattr(sys, "stdin", io.StringIO("/exit\n"))

    await asyncio.wait_for(cli_module.run_cli(_settings()), timeout=5.0)

    assert log.closed is True


class _NeverReturningStdin:
    """readline() blocks forever -- stands in for an interactive terminal with nothing typed."""

    def readline(self) -> str:
        time.sleep(1000)
        return ""  # pragma: no cover -- never reached in the test


async def test_stdin_loop_is_cancellable(monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _build_test_runtime()
    task = asyncio.ensure_future(cli_module._stdin_loop(rt, stdin=_NeverReturningStdin()))
    await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
