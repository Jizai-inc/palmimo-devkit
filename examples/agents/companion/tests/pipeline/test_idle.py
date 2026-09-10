"""Tests for :mod:`palmimo_companion_agent.pipeline.idle` (a compute-only Palmimo + a fake LlmProvider; no network)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

import palmimo_companion_agent.pipeline.dispatch as dispatch_module
import palmimo_companion_agent.pipeline.idle as idle_module
from palmimo_companion_agent.core.toolview import IDLE_TOOL_NAMES, ToolView
from palmimo_companion_agent.pipeline.bus import Bus
from palmimo_companion_agent.pipeline.history import (
    AgentThoughtEvent,
    CameraEvent,
    History,
    SystemNoteEvent,
    ToolExecEvent,
)
from palmimo_companion_agent.pipeline.idle import IdleTurn
from palmimo_sdk.agent.toolset import AgentToolSet


def _idle_view(toolset: AgentToolSet) -> ToolView:
    return ToolView(toolset, allow=IDLE_TOOL_NAMES, squash_say=True)


async def test_tick_advertises_only_the_idle_toolset(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable, events_of_type: Callable
) -> None:
    llm = fake_llm_provider([tool_call_turn("nod", {"seconds": 1.0, "reason": "why not"})])
    turn = IdleTurn(History(), _idle_view(toolset), llm, Bus())

    await turn.tick()

    [call] = llm.calls
    names = {tool["function"]["name"] for tool in call["tools"]}
    assert names == IDLE_TOOL_NAMES


async def test_tick_asks_the_llm_for_a_single_tool_call_continuation(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable
) -> None:
    """idle's continuation nudge (window ends on a tool turn) states the single-tool
    contract -- unlike respond's up-to-4-plan wording."""
    history = History()
    llm = fake_llm_provider([tool_call_turn("nod", {"seconds": 1.0, "reason": "again"})])
    turn = IdleTurn(history, _idle_view(toolset), llm, Bus())
    history.add(
        ToolExecEvent(tool_call_id="c", name="rest", arguments="{}", result="rested")
    )  # window already ends on a tool turn

    await turn.tick()

    [call] = llm.calls
    user_contents = [m["content"] for m in call["messages"] if m["role"] == "user"]
    assert any("single tool call" in c for c in user_contents)


async def test_tick_executes_the_first_tool_call_and_records_it(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([tool_call_turn("rest", {"seconds": 0.5, "reason": "taking a breath"})])
    turn = IdleTurn(history, _idle_view(toolset), llm, Bus())

    await turn.tick()

    [tool_event] = events_of_type(history, ToolExecEvent)
    assert tool_event.name == "rest"
    assert tool_event.thought == "taking a breath"


async def test_tick_squashes_say_on_the_idle_view(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([tool_call_turn("nod", {"seconds": 1.0, "say": "hi", "reason": "greeting"})])
    turn = IdleTurn(history, _idle_view(toolset), llm, Bus())

    await turn.tick()

    [tool_event] = events_of_type(history, ToolExecEvent)
    assert "no speaker attached" not in tool_event.result
    assert "said" not in tool_event.result


async def test_tick_with_no_tool_call_records_thought_and_nudge(
    toolset: AgentToolSet, fake_llm_provider: type, text_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([text_turn("just thinking out loud")])
    turn = IdleTurn(history, _idle_view(toolset), llm, Bus())

    await turn.tick()

    [thought] = events_of_type(history, AgentThoughtEvent)
    assert thought.text == "just thinking out loud"
    notes = [e.text for e in events_of_type(history, SystemNoteEvent)]
    assert idle_module._NO_TOOL_CALL_NOTE in notes
    assert "exactly one tool" in idle_module._NO_TOOL_CALL_NOTE


async def test_tick_with_neither_content_nor_tool_call_becomes_a_system_note(
    toolset: AgentToolSet, fake_llm_provider: type, text_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([text_turn(None)])
    turn = IdleTurn(history, _idle_view(toolset), llm, Bus())

    await turn.tick()

    notes = [e.text for e in events_of_type(history, SystemNoteEvent)]
    assert dispatch_module._EMPTY_RESPONSE_NOTE in notes
    assert events_of_type(history, AgentThoughtEvent) == []


async def test_tick_on_llm_exception_records_a_note_and_backs_off(
    toolset: AgentToolSet, fake_llm_provider: type, events_of_type: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatch_module, "_LLM_ERROR_BACKOFF_SECONDS", 0.0)
    history = History()
    llm = fake_llm_provider([RuntimeError("simulated provider outage")])
    turn = IdleTurn(history, _idle_view(toolset), llm, Bus())

    await turn.tick()

    error_notes = [e.text for e in events_of_type(history, SystemNoteEvent) if "simulated provider outage" in e.text]
    assert error_notes


async def test_tick_on_empty_choices_records_a_note_and_backs_off(
    toolset: AgentToolSet,
    fake_llm_provider: type,
    empty_choices_turn: Callable,
    events_of_type: Callable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dispatch_module, "_LLM_ERROR_BACKOFF_SECONDS", 0.0)
    history = History()
    llm = fake_llm_provider([empty_choices_turn()])
    turn = IdleTurn(history, _idle_view(toolset), llm, Bus())

    await turn.tick()

    assert any(e.text == dispatch_module._EMPTY_CHOICES_NOTE for e in events_of_type(history, SystemNoteEvent))


async def test_tick_with_malformed_tool_arguments_becomes_an_observation_not_a_crash(
    toolset: AgentToolSet, fake_llm_provider: type, raw_tool_call_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([raw_tool_call_turn("nod", '{"seconds": 1.0)}")')])
    turn = IdleTurn(history, _idle_view(toolset), llm, Bus())

    await turn.tick()

    [tool_event] = events_of_type(history, ToolExecEvent)
    assert tool_event.name == "nod"
    assert "execution error" in tool_event.result
    assert "JSON" in tool_event.result
    assert tool_event.error is True


async def test_tick_with_missing_reason_falls_back_to_message_content(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([tool_call_turn("rest", {"seconds": 0.5}, content="resting on a whim")])
    turn = IdleTurn(history, _idle_view(toolset), llm, Bus())

    await turn.tick()

    [tool_event] = events_of_type(history, ToolExecEvent)
    assert "execution error" not in tool_event.result
    assert tool_event.error is False
    assert tool_event.thought == "resting on a whim"


async def test_a_tool_result_with_images_is_described_and_recorded_as_a_camera_event(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([tool_call_turn("capture", {"reason": "curious"})])
    view = _idle_view(toolset)
    result_images = [b"fake-jpeg-bytes"]
    original_call = view.call

    async def _fake_call(name: str, args: dict) -> Any:
        from palmimo_sdk.agent.tools import ToolResult

        if name == "capture":
            return ToolResult(text="captured image from head camera", images=result_images)
        return await original_call(name, args)

    view.call = _fake_call
    turn = IdleTurn(history, view, llm, Bus())

    await turn.tick()

    [camera_event] = events_of_type(history, CameraEvent)
    assert camera_event.summary == "a fake scene"
    import base64

    assert llm.described == [base64.b64encode(b"fake-jpeg-bytes").decode("ascii")]


async def test_tick_disallowed_tool_call_is_reported_not_executed(
    toolset: AgentToolSet, fake_llm_provider: type, tool_call_turn: Callable, events_of_type: Callable
) -> None:
    """The idle view is restricted -- a hallucinated out-of-scope tool call
    must come back as an observation, not silently run through the full toolset."""
    history = History()
    llm = fake_llm_provider([tool_call_turn("dance", {"seconds": 1.0, "reason": "excited"})])
    turn = IdleTurn(history, _idle_view(toolset), llm, Bus())

    await turn.tick()

    [tool_event] = events_of_type(history, ToolExecEvent)
    assert "not available" in tool_event.result
    # A genuine ToolResult.is_error=True (ToolView.call's own disallowed-tool
    # result), not just the "execution error:" string dispatch spells for a
    # local failure -- proves ToolExecEvent.error tracks the flag, not text.
    assert tool_event.error is True
