"""Tests for :mod:`palmimo_companion_agent.pipeline.respond` (a compute-only Palmimo + a fake LlmProvider; no network)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

import palmimo_companion_agent.pipeline.dispatch as dispatch_module
import palmimo_companion_agent.pipeline.respond as respond_module
from palmimo_companion_agent.core.toolview import ToolView
from palmimo_companion_agent.pipeline.bus import Bus
from palmimo_companion_agent.pipeline.history import (
    AgentThoughtEvent,
    CameraEvent,
    History,
    SystemNoteEvent,
    ToolExecEvent,
)
from palmimo_companion_agent.pipeline.respond import RespondTurn
from palmimo_sdk.agent.toolset import AgentToolSet


def _respond_view(toolset: AgentToolSet) -> ToolView:
    return ToolView(toolset, allow=None, squash_say=False)


async def test_run_passes_parallel_tool_calls_true_and_the_full_toolset(
    toolset: AgentToolSet, fake_llm_provider: type, plan_turn: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([plan_turn([("nod", {"seconds": 1.0, "reason": "greet"})])])
    turn = RespondTurn(history, _respond_view(toolset), llm, Bus())

    await turn.run()

    [call] = llm.calls
    assert call["parallel_tool_calls"] is True
    names = {tool["function"]["name"] for tool in call["tools"]}
    assert "dance" in names
    assert "say" in names


async def test_run_asks_the_llm_for_an_up_to_four_call_plan_continuation(
    toolset: AgentToolSet, fake_llm_provider: type, plan_turn: Callable
) -> None:
    """respond's continuation nudge (window ends on a tool turn) states the up-to-4-plan
    contract -- unlike idle's single-tool wording, which would contradict it here."""
    history = History()
    history.add(ToolExecEvent(tool_call_id="c", name="rest", arguments="{}", result="rested"))
    llm = fake_llm_provider([plan_turn([("nod", {"seconds": 1.0, "reason": "again"})])])
    turn = RespondTurn(history, _respond_view(toolset), llm, Bus())

    await turn.run()

    [call] = llm.calls
    user_contents = [m["content"] for m in call["messages"] if m["role"] == "user"]
    assert any("up to 4 tool calls" in c for c in user_contents)


async def test_run_with_directed_speech_true_appends_the_nudge_to_the_chat_call_only(
    toolset: AgentToolSet, fake_llm_provider: type, plan_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([plan_turn([("nod", {"seconds": 1.0, "reason": "greet"})])])
    turn = RespondTurn(history, _respond_view(toolset), llm, Bus())

    await turn.run(directed_speech=True)

    [call] = llm.calls
    user_contents = [m["content"] for m in call["messages"] if m["role"] == "user"]
    assert any(respond_module._DIRECTED_SPEECH_NUDGE in c for c in user_contents)
    # Ephemeral: never stored in history, unlike every other note this turn records.
    notes = [e.text for e in events_of_type(history, SystemNoteEvent)]
    assert not any(respond_module._DIRECTED_SPEECH_NUDGE in n for n in notes)


async def test_run_with_directed_speech_false_omits_the_nudge(
    toolset: AgentToolSet, fake_llm_provider: type, plan_turn: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([plan_turn([("nod", {"seconds": 1.0, "reason": "greet"})])])
    turn = RespondTurn(history, _respond_view(toolset), llm, Bus())

    await turn.run(directed_speech=False)

    [call] = llm.calls
    user_contents = [m["content"] for m in call["messages"] if m["role"] == "user"]
    assert not any(respond_module._DIRECTED_SPEECH_NUDGE in c for c in user_contents)


async def test_run_executes_multiple_tool_calls_in_order(
    toolset: AgentToolSet, fake_llm_provider: type, plan_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    llm = fake_llm_provider(
        [
            plan_turn(
                [
                    ("nod", {"seconds": 0.5, "reason": "answer"}),
                    ("say", {"text": "hi there"}),
                ]
            )
        ]
    )
    turn = RespondTurn(history, _respond_view(toolset), llm, Bus())

    await turn.run()

    events = events_of_type(history, ToolExecEvent)
    assert [e.name for e in events] == ["nod", "say"]


async def test_run_records_plan_accompanying_content_once_as_a_thought_not_per_call(
    toolset: AgentToolSet, fake_llm_provider: type, plan_turn: Callable, events_of_type: Callable
) -> None:
    """A plan can carry BOTH assistant content and tool_calls. Unlike the old behavior this
    replaces (silently dropping that content), it is now recorded once, up front, as its own
    AgentThoughtEvent -- not attached as every call's `thought` (each call keeps its own
    `reason` for that)."""
    history = History()
    llm = fake_llm_provider(
        [
            plan_turn(
                [
                    ("nod", {"seconds": 0.5, "reason": "answering yes"}),
                    ("say", {"text": "hi there"}),
                ],
                content="greeting them warmly",
            )
        ]
    )
    turn = RespondTurn(history, _respond_view(toolset), llm, Bus())

    await turn.run()

    [thought] = events_of_type(history, AgentThoughtEvent)
    assert thought.text == "greeting them warmly"
    tool_events = events_of_type(history, ToolExecEvent)
    assert tool_events[0].thought == "answering yes"  # each call's own `reason`, not the shared content
    assert tool_events[1].thought is None  # "say" had no `reason` and does NOT fall back to the shared content


async def test_run_records_no_thought_when_the_plan_carries_no_content(
    toolset: AgentToolSet, fake_llm_provider: type, plan_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([plan_turn([("say", {"text": "hi"})])])  # no content, no reason
    turn = RespondTurn(history, _respond_view(toolset), llm, Bus())

    await turn.run()

    assert events_of_type(history, AgentThoughtEvent) == []
    [tool_event] = events_of_type(history, ToolExecEvent)
    assert tool_event.thought is None


async def test_run_caps_at_four_tool_calls_with_a_truncation_note(
    toolset: AgentToolSet, fake_llm_provider: type, plan_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    calls = [("nod", {"seconds": 0.5, "reason": f"n{i}"}) for i in range(6)]
    llm = fake_llm_provider([plan_turn(calls)])
    turn = RespondTurn(history, _respond_view(toolset), llm, Bus())

    await turn.run()

    events = events_of_type(history, ToolExecEvent)
    assert len(events) == 4
    notes = [e.text for e in events_of_type(history, SystemNoteEvent)]
    assert any("only the first 4" in n for n in notes)


async def test_run_continues_past_a_malformed_middle_call(
    toolset: AgentToolSet,
    fake_llm_provider: type,
    plan_turn_with_raw_arguments: Callable,
    events_of_type: Callable,
) -> None:
    history = History()
    llm = fake_llm_provider(
        [
            plan_turn_with_raw_arguments(
                [
                    ("nod", {"seconds": 0.5, "reason": "first"}),
                    ("rest", '{"seconds": 1.0)}")'),  # malformed JSON
                    ("nod", {"seconds": 0.5, "reason": "third"}),
                ]
            )
        ]
    )
    turn = RespondTurn(history, _respond_view(toolset), llm, Bus())

    await turn.run()

    events = events_of_type(history, ToolExecEvent)
    assert [e.name for e in events] == ["nod", "rest", "nod"]
    assert "execution error" in events[1].result
    assert "JSON" in events[1].result
    assert "execution error" not in events[0].result
    assert "execution error" not in events[2].result


async def test_run_stops_the_remainder_when_cancel_is_set_mid_plan(
    toolset: AgentToolSet, fake_llm_provider: type, plan_turn: Callable, events_of_type: Callable
) -> None:
    # set_face is deliberately NOT long-running, so run_tool's dispatch never
    # races it against bus.cancel and never clears the flag on its own --
    # only RespondTurn's own pre-call check (before the 2nd/3rd call) does,
    # so this isolates that check.
    history = History()
    bus = Bus()
    llm = fake_llm_provider(
        [
            plan_turn(
                [
                    ("set_face", {"name": "HAPPY", "reason": "first"}),
                    ("set_face", {"name": "SAD", "reason": "second"}),
                    ("set_face", {"name": "ANGRY", "reason": "third"}),
                ]
            )
        ]
    )
    view = _respond_view(toolset)
    original_call = view.call

    async def _fake_call(name: str, args: dict) -> Any:
        bus.cancel.set()  # simulate new input arriving right after the first call
        return await original_call(name, args)

    view.call = _fake_call
    turn = RespondTurn(history, view, llm, bus)

    await turn.run()

    events = events_of_type(history, ToolExecEvent)
    assert len(events) == 1
    notes = [e.text for e in events_of_type(history, SystemNoteEvent)]
    assert any("discarded" in n for n in notes)


async def test_run_discards_the_whole_plan_when_cancel_is_already_set(
    toolset: AgentToolSet, fake_llm_provider: type, plan_turn: Callable, events_of_type: Callable
) -> None:
    """An interrupt that arrived while the chat() call itself was in flight leaves
    bus.cancel already set before run() ever starts executing the plan. Nothing in
    that stale plan may run, and the flag must be left set (not cleared here) -- the
    conductor's own drain owns consuming it on the next loop iteration."""
    history = History()
    bus = Bus()
    llm = fake_llm_provider(
        [
            plan_turn(
                [
                    ("set_face", {"name": "HAPPY", "reason": "first"}),
                    ("set_face", {"name": "SAD", "reason": "second"}),
                ]
            )
        ]
    )
    bus.cancel.set()  # an interrupt arrived while the chat() call above was in flight
    turn = RespondTurn(history, _respond_view(toolset), llm, bus)

    await turn.run()

    assert events_of_type(history, ToolExecEvent) == []
    notes = [e.text for e in events_of_type(history, SystemNoteEvent)]
    assert any("discarded" in n for n in notes)
    assert bus.cancel.is_set()  # never cleared here -- the conductor's drain owns that


async def test_run_discards_the_remainder_when_the_first_long_running_call_is_interrupted(
    toolset: AgentToolSet, fake_llm_provider: type, plan_turn: Callable, events_of_type: Callable
) -> None:
    """A cancel arriving WHILE the first (long-running) planned call is executing loses
    the race inside dispatch._run_cancellable, which clears bus.cancel itself after
    aborting the motion -- so the pre-call check ahead of the 2nd call would never see
    it set. The post-call "did this come back interrupted" check must catch it instead."""
    history = History()
    bus = Bus()
    llm = fake_llm_provider(
        [
            plan_turn(
                [
                    ("forward", {"seconds": 8, "reason": "first"}),
                    ("nod", {"seconds": 0.5, "reason": "second"}),
                ]
            )
        ]
    )
    turn = RespondTurn(history, _respond_view(toolset), llm, bus)

    task = asyncio.ensure_future(turn.run())
    await asyncio.sleep(0.05)  # let forward() start
    bus.cancel.set()
    await asyncio.wait_for(task, timeout=3.0)

    events = events_of_type(history, ToolExecEvent)
    assert [e.name for e in events] == ["forward"]
    assert "interrupted" in events[0].result
    notes = [e.text for e in events_of_type(history, SystemNoteEvent)]
    assert any("discarded" in n for n in notes)


async def test_run_with_no_tool_call_records_thought_and_nudge(
    toolset: AgentToolSet, fake_llm_provider: type, text_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([text_turn("just thinking out loud")])
    turn = RespondTurn(history, _respond_view(toolset), llm, Bus())

    await turn.run()

    [thought] = events_of_type(history, AgentThoughtEvent)
    assert thought.text == "just thinking out loud"
    notes = [e.text for e in events_of_type(history, SystemNoteEvent)]
    assert respond_module._NO_TOOL_CALL_NOTE in notes
    assert "one or several (up to 4)" in respond_module._NO_TOOL_CALL_NOTE


async def test_run_with_neither_content_nor_tool_call_becomes_a_system_note(
    toolset: AgentToolSet, fake_llm_provider: type, text_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([text_turn(None)])
    turn = RespondTurn(history, _respond_view(toolset), llm, Bus())

    await turn.run()

    notes = [e.text for e in events_of_type(history, SystemNoteEvent)]
    assert dispatch_module._EMPTY_RESPONSE_NOTE in notes


async def test_run_on_llm_exception_records_a_note_and_backs_off(
    toolset: AgentToolSet, fake_llm_provider: type, events_of_type: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatch_module, "_LLM_ERROR_BACKOFF_SECONDS", 0.0)
    history = History()
    llm = fake_llm_provider([RuntimeError("simulated provider outage")])
    turn = RespondTurn(history, _respond_view(toolset), llm, Bus())

    await turn.run()

    error_notes = [e.text for e in events_of_type(history, SystemNoteEvent) if "simulated provider outage" in e.text]
    assert error_notes


async def test_run_on_empty_choices_records_a_note_and_backs_off(
    toolset: AgentToolSet,
    fake_llm_provider: type,
    empty_choices_turn: Callable,
    events_of_type: Callable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dispatch_module, "_LLM_ERROR_BACKOFF_SECONDS", 0.0)
    history = History()
    llm = fake_llm_provider([empty_choices_turn()])
    turn = RespondTurn(history, _respond_view(toolset), llm, Bus())

    await turn.run()

    assert any(e.text == dispatch_module._EMPTY_CHOICES_NOTE for e in events_of_type(history, SystemNoteEvent))


async def test_a_tool_result_with_images_is_described_and_recorded_as_a_camera_event(
    toolset: AgentToolSet, fake_llm_provider: type, plan_turn: Callable, events_of_type: Callable
) -> None:
    history = History()
    llm = fake_llm_provider([plan_turn([("capture", {"reason": "curious"})])])
    view = _respond_view(toolset)
    result_images = [b"fake-jpeg-bytes"]
    original_call = view.call

    async def _fake_call(name: str, args: dict) -> Any:
        from palmimo_sdk.agent.tools import ToolResult

        if name == "capture":
            return ToolResult(text="captured image from head camera", images=result_images)
        return await original_call(name, args)

    view.call = _fake_call
    turn = RespondTurn(history, view, llm, Bus())

    await turn.run()

    [camera_event] = events_of_type(history, CameraEvent)
    assert camera_event.summary == "a fake scene"
