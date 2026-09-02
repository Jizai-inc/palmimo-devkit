"""Tests for :mod:`palmimo_companion_agent.pipeline.conductor` (a compute-only Palmimo + a fake LlmProvider; no network)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

import pytest

import palmimo_companion_agent.pipeline.conductor as conductor_module
import palmimo_companion_agent.pipeline.respond as respond_module
from palmimo_companion_agent.pipeline.bus import Bus
from palmimo_companion_agent.pipeline.conductor import Conductor
from palmimo_companion_agent.pipeline.history import (
    History,
    KeyboardEvent,
    MicInputEvent,
    SystemNoteEvent,
    ToolExecEvent,
)
from palmimo_companion_agent.pipeline.llm import SpeechVerdict
from palmimo_sdk.agent.toolset import AgentToolSet


@pytest.fixture(autouse=True)
def _fast_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Idle ticks would otherwise wait 2-6s between them -- shrink that so tests run in milliseconds."""
    monkeypatch.setattr(conductor_module, "_IDLE_PAUSE_MIN_SECONDS", 0.01)
    monkeypatch.setattr(conductor_module, "_IDLE_PAUSE_MAX_SECONDS", 0.02)


async def _run_briefly(conductor: Conductor, predicate: Any, *, timeout: float = 1.0) -> None:
    """Run conductor.run() as a background task until *predicate* is true, then cancel it."""
    task = asyncio.ensure_future(conductor.run())
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.005)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ----------------------------------------------------------------------
# submit_speech routing (no run() needed -- just the bus)
# ----------------------------------------------------------------------


def test_submit_speech_drops_noise_without_posting_anything(toolset: AgentToolSet, fake_llm_provider: type) -> None:
    history = History()
    bus = Bus()
    conductor = Conductor(history, toolset, fake_llm_provider(), bus)
    conductor.submit_speech(SpeechVerdict(kind="noise", text=None), "uhh")
    assert bus.drain() == []


def test_submit_speech_logs_the_dropped_noise(
    toolset: AgentToolSet, fake_llm_provider: type, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    history = History()
    bus = Bus()
    conductor = Conductor(history, toolset, fake_llm_provider(), bus)

    conductor.submit_speech(SpeechVerdict(kind="noise", text=None), "uhh")

    assert any("noise" in record.message and "uhh" in record.message for record in caplog.records)


def test_submit_speech_command_cancels_running_behavior(toolset: AgentToolSet, fake_llm_provider: type) -> None:
    history = History()
    bus = Bus()
    conductor = Conductor(history, toolset, fake_llm_provider(), bus)
    conductor.submit_speech(SpeechVerdict(kind="command", text="dance"), "dance")
    [interrupt] = bus.drain()
    assert interrupt.kind == "speech"
    assert interrupt.text == "dance"
    assert interrupt.cancel_running is True
    assert interrupt.directed is True


def test_submit_speech_question_is_directed_too(toolset: AgentToolSet, fake_llm_provider: type) -> None:
    history = History()
    bus = Bus()
    conductor = Conductor(history, toolset, fake_llm_provider(), bus)
    conductor.submit_speech(SpeechVerdict(kind="question", text="what time is it?"), "what time is it?")
    [interrupt] = bus.drain()
    assert interrupt.cancel_running is True
    assert interrupt.directed is True


def test_submit_speech_ambient_does_not_cancel_running_behavior(toolset: AgentToolSet, fake_llm_provider: type) -> None:
    history = History()
    bus = Bus()
    conductor = Conductor(history, toolset, fake_llm_provider(), bus)
    conductor.submit_speech(SpeechVerdict(kind="ambient", text="nice weather"), "nice weather")
    [interrupt] = bus.drain()
    assert interrupt.cancel_running is False
    assert interrupt.directed is False


# ----------------------------------------------------------------------
# submit_speech barge-in: a command/question kicks cancel_running() right
# away, not just via the posted Interrupt.
# ----------------------------------------------------------------------


async def test_submit_speech_command_triggers_cancel_running_immediately(
    toolset: AgentToolSet, fake_llm_provider: type
) -> None:
    calls = 0

    async def fake_cancel_running() -> None:
        nonlocal calls
        calls += 1

    toolset.cancel_running = fake_cancel_running  # type: ignore[method-assign]
    history = History()
    bus = Bus()
    conductor = Conductor(history, toolset, fake_llm_provider(), bus)

    conductor.submit_speech(SpeechVerdict(kind="command", text="dance"), "dance")
    await asyncio.sleep(0)  # let the scheduled cancel_running() task run

    assert calls == 1


async def test_submit_speech_ambient_does_not_trigger_cancel_running(
    toolset: AgentToolSet, fake_llm_provider: type
) -> None:
    calls = 0

    async def fake_cancel_running() -> None:
        nonlocal calls
        calls += 1

    toolset.cancel_running = fake_cancel_running  # type: ignore[method-assign]
    history = History()
    bus = Bus()
    conductor = Conductor(history, toolset, fake_llm_provider(), bus)

    conductor.submit_speech(SpeechVerdict(kind="ambient", text="nice weather"), "nice weather")
    await asyncio.sleep(0)

    assert calls == 0


async def test_barge_in_logs_a_failing_cancel_running_task(
    toolset: AgentToolSet, fake_llm_provider: type, caplog: pytest.LogCaptureFixture
) -> None:
    """The fire-and-forget _barge_in() task's exception must not vanish silently -- nothing else awaits it."""

    async def failing_cancel_running() -> None:
        raise RuntimeError("boom")

    toolset.cancel_running = failing_cancel_running  # type: ignore[method-assign]
    history = History()
    bus = Bus()
    conductor = Conductor(history, toolset, fake_llm_provider(), bus)

    with caplog.at_level(logging.WARNING):
        conductor.submit_speech(SpeechVerdict(kind="command", text="dance"), "dance")
        await asyncio.sleep(0)  # let the scheduled task run and fail
        await asyncio.sleep(0)  # let its done callbacks run

    assert any("boom" in record.message for record in caplog.records)


def test_submit_speech_command_before_event_loop_does_not_raise(toolset: AgentToolSet, fake_llm_provider: type) -> None:
    """A synchronous caller with no running event loop (e.g. this test itself)
    must not crash -- best-effort, nothing is in flight to interrupt yet anyway."""
    history = History()
    bus = Bus()
    conductor = Conductor(history, toolset, fake_llm_provider(), bus)
    conductor.submit_speech(SpeechVerdict(kind="command", text="dance"), "dance")  # must not raise


# ----------------------------------------------------------------------
# run() -- startup note, respond-over-idle prioritization, idle pacing
# ----------------------------------------------------------------------


async def test_run_adds_a_startup_note_before_anything_else(
    toolset: AgentToolSet, fake_llm_provider: type, events_of_type: Callable
) -> None:
    history = History()
    conductor = Conductor(history, toolset, fake_llm_provider(), Bus())

    await _run_briefly(conductor, lambda: events_of_type(history, SystemNoteEvent))

    assert isinstance(history.events[0], SystemNoteEvent)


async def test_a_queued_keyboard_instruction_runs_a_respond_turn_not_idle(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    bus = Bus()
    llm = fake_llm_provider([tool_call_turn("nod", {"seconds": 0.5, "reason": "answer"})])
    conductor = Conductor(history, toolset, llm, bus)
    conductor.submit_user_text("hello there")

    # nod's own duration plus the SDK's post-motion stance settle (see
    # palmimo_sdk.agent.tools._STANCE_SETTLE_SECONDS) comfortably exceeds
    # _run_briefly's default 1.0s timeout.
    await _run_briefly(conductor, lambda: events_of_type(history, ToolExecEvent), timeout=3.0)

    [tool_event] = events_of_type(history, ToolExecEvent)
    assert tool_event.name == "nod"
    assert isinstance(history.events[1], KeyboardEvent)


async def test_a_keyboard_only_turn_runs_respond_without_the_directed_speech_nudge(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    bus = Bus()
    llm = fake_llm_provider([tool_call_turn("nod", {"seconds": 0.5, "reason": "answer"})])
    conductor = Conductor(history, toolset, llm, bus)
    conductor.submit_user_text("hello there")

    await _run_briefly(conductor, lambda: events_of_type(history, ToolExecEvent), timeout=3.0)

    # The respond turn's chat() call is the first -- later calls (if any,
    # from the loop moving on to idle ticks before this task is cancelled)
    # aren't of interest here.
    call = llm.calls[0]
    user_contents = [m["content"] for m in call["messages"] if m["role"] == "user"]
    assert not any(respond_module._DIRECTED_SPEECH_NUDGE in c for c in user_contents)


async def test_a_directed_speech_interrupt_runs_respond_with_the_nudge(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    bus = Bus()
    llm = fake_llm_provider([tool_call_turn("nod", {"seconds": 0.5, "reason": "answer"})])
    conductor = Conductor(history, toolset, llm, bus)
    conductor.submit_speech(SpeechVerdict(kind="command", text="hello there"), "hello there")

    await _run_briefly(conductor, lambda: events_of_type(history, ToolExecEvent), timeout=3.0)

    call = llm.calls[0]
    user_contents = [m["content"] for m in call["messages"] if m["role"] == "user"]
    assert any(respond_module._DIRECTED_SPEECH_NUDGE in c for c in user_contents)


async def test_an_ambient_only_batch_runs_respond_without_the_directed_speech_nudge(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable, events_of_type: Callable
) -> None:
    """Ambient speech still reaches a respond turn (the model may still want to react to it),
    but -- unlike directed speech -- it must NOT get the must-speak nudge (respond.md's
    [overheard] rule makes a spoken reply optional there)."""
    history = History()
    bus = Bus()
    llm = fake_llm_provider([tool_call_turn("set_face", {"name": "CURIOUS", "reason": "overheard something"})])
    conductor = Conductor(history, toolset, llm, bus)
    conductor.submit_speech(SpeechVerdict(kind="ambient", text="I'm hungry"), "I'm hungry")

    await _run_briefly(conductor, lambda: events_of_type(history, ToolExecEvent), timeout=3.0)

    [mic_event] = events_of_type(history, MicInputEvent)
    assert mic_event.directed is False

    call = llm.calls[0]
    user_contents = [m["content"] for m in call["messages"] if m["role"] == "user"]
    assert not any(respond_module._DIRECTED_SPEECH_NUDGE in c for c in user_contents)


async def test_respond_is_prioritized_over_idle_when_speech_is_queued(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable, events_of_type: Callable
) -> None:
    """Even with an idle-eligible turn available, queued speech/keyboard must be served first."""
    history = History()
    bus = Bus()
    llm = fake_llm_provider([tool_call_turn("dance", {"seconds": 0.5, "reason": "celebrate"})])
    conductor = Conductor(history, toolset, llm, bus)
    conductor.submit_user_text("dance for me")

    # dance's own duration plus its post-motion settle comfortably exceeds
    # _run_briefly's default 1.0s timeout (see the note above).
    await _run_briefly(conductor, lambda: events_of_type(history, ToolExecEvent), timeout=3.0)

    [tool_event] = events_of_type(history, ToolExecEvent)
    # "dance" is outside the idle allowlist -- only the respond view can run it.
    assert tool_event.name == "dance"
    assert "not available" not in tool_event.result


async def test_idle_ticks_run_when_nothing_is_queued(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([tool_call_turn("rest", {"seconds": 0.5, "reason": "quiet moment"})])
    conductor = Conductor(history, toolset, llm, Bus())

    await _run_briefly(conductor, lambda: events_of_type(history, ToolExecEvent), timeout=3.0)

    [tool_event] = events_of_type(history, ToolExecEvent)
    assert tool_event.name == "rest"


async def test_interrupts_arriving_during_the_idle_pause_are_not_lost(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    bus = Bus()
    llm = fake_llm_provider([tool_call_turn("nod", {"seconds": 0.5, "reason": "answer"})])
    conductor = Conductor(history, toolset, llm, bus)

    task = asyncio.ensure_future(conductor.run())
    try:
        await asyncio.sleep(0.005)  # let the loop reach the idle pause (nothing queued yet)
        conductor.submit_user_text("hello during the pause")
        async with asyncio.timeout(2.0):
            while not events_of_type(history, KeyboardEvent):
                await asyncio.sleep(0.005)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert any(e.text == "hello during the pause" for e in events_of_type(history, KeyboardEvent))


# ----------------------------------------------------------------------
# hear()
# ----------------------------------------------------------------------


async def test_hear_classifies_and_posts_the_verdict(toolset: AgentToolSet, fake_llm_provider: type) -> None:
    history = History()
    bus = Bus()
    llm = fake_llm_provider()
    llm.classify_result = SpeechVerdict(kind="question", text="is it sunny?")
    conductor = Conductor(history, toolset, llm, bus)

    await conductor.hear("is it sunny")

    assert llm.classify_calls == ["is it sunny"]
    [interrupt] = bus.drain()
    assert interrupt.kind == "speech"
    assert interrupt.text == "is it sunny?"


async def test_hear_falls_back_to_command_on_classify_failure(toolset: AgentToolSet, fake_llm_provider: type) -> None:
    history = History()
    bus = Bus()
    llm = fake_llm_provider()
    llm.classify_result = RuntimeError("guard is down")
    conductor = Conductor(history, toolset, llm, bus)

    await conductor.hear("some raw text")

    [interrupt] = bus.drain()
    assert interrupt.kind == "speech"
    assert interrupt.text == "some raw text"
    assert interrupt.cancel_running is True  # "command" cancels running behavior


async def test_hear_drops_noise(toolset: AgentToolSet, fake_llm_provider: type) -> None:
    history = History()
    bus = Bus()
    llm = fake_llm_provider()
    llm.classify_result = SpeechVerdict(kind="noise", text=None)
    conductor = Conductor(history, toolset, llm, bus)

    await conductor.hear("uhh")

    assert bus.drain() == []


# ----------------------------------------------------------------------
# Sleep gating -- a successful `sleep` tool result must stop idle ticks
# ----------------------------------------------------------------------


async def test_a_successful_sleep_stops_idle_ticks(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([tool_call_turn("rest", {"seconds": 0.5, "reason": "quiet moment"})])
    conductor = Conductor(history, toolset, llm, Bus())
    # Simulate a completed sleep call landing in history -- the same
    # ToolExecEvent execute_and_record would add, observed by the Conductor's
    # own Sleeping subscriber.
    history.add(ToolExecEvent(tool_call_id="t1", name="sleep", arguments="{}", result="went to sleep"))

    task = asyncio.ensure_future(conductor.run())
    try:
        await asyncio.sleep(0.1)  # several idle-pause cycles under _fast_pacing
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # The idle turn never asked the LLM for anything -- ticks were skipped outright.
    assert llm.calls == []


async def test_idle_resumes_after_a_successful_wake_up(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([tool_call_turn("rest", {"seconds": 0.5, "reason": "quiet moment"})])
    conductor = Conductor(history, toolset, llm, Bus())
    history.add(ToolExecEvent(tool_call_id="t1", name="sleep", arguments="{}", result="went to sleep"))
    history.add(ToolExecEvent(tool_call_id="t2", name="wake_up", arguments="{}", result="woke up and stretched"))

    def _rest_ran() -> bool:
        return any(event.name == "rest" for event in events_of_type(history, ToolExecEvent))

    await _run_briefly(conductor, _rest_ran, timeout=3.0)

    assert _rest_ran()


async def test_a_failed_sleep_does_not_gate_idle_ticks(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([tool_call_turn("rest", {"seconds": 0.5, "reason": "quiet moment"})])
    conductor = Conductor(history, toolset, llm, Bus())
    history.add(
        ToolExecEvent(
            tool_call_id="t1", name="sleep", arguments="{}", result="execution error: servo fault", error=True
        )
    )

    def _rest_ran() -> bool:
        return any(event.name == "rest" for event in events_of_type(history, ToolExecEvent))

    await _run_briefly(conductor, _rest_ran, timeout=3.0)

    assert _rest_ran()


async def test_a_failed_wake_up_does_not_resume_idle_ticks(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([tool_call_turn("rest", {"seconds": 0.5, "reason": "quiet moment"})])
    conductor = Conductor(history, toolset, llm, Bus())
    history.add(ToolExecEvent(tool_call_id="t1", name="sleep", arguments="{}", result="went to sleep"))
    history.add(
        ToolExecEvent(
            tool_call_id="t2", name="wake_up", arguments="{}", result="execution error: servo fault", error=True
        )
    )

    task = asyncio.ensure_future(conductor.run())
    try:
        await asyncio.sleep(0.1)  # several idle-pause cycles under _fast_pacing
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # The mirror is still asleep -- a failed wake_up must not resume idle ticks.
    assert llm.calls == []
