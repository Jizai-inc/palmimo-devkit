"""Tests for :mod:`palmimo_companion_agent.realtime.bridge` -- ToolBridge.

Uses a scripted fake in place of :class:`~palmimo_sdk.agent.toolset.AgentToolSet`
(cast past the strict type, the same pattern the pipeline's own tests use for
a monkeypatched toolset method) so plan-execution timing is fully test-driven
instead of racing real motion durations.
"""

from __future__ import annotations

import asyncio
import io
import sys
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from palmimo_companion_agent.realtime.bridge import ToolBridge
from palmimo_companion_agent.realtime.log import EventLog
from palmimo_companion_agent.realtime.protocol import ItemCreate, Response, ResponseCreate
from palmimo_companion_agent.realtime.state import Sleeping
from palmimo_sdk.agent.tools import ToolResult
from palmimo_sdk.agent.toolset import AgentToolSet


class ScriptedToolset:
    """A toolset stand-in whose :meth:`call` a test can gate open/closed for precise interleaving."""

    def __init__(self, results: dict[str, ToolResult] | None = None) -> None:
        self.results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.cancels = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.gated = False

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        self.calls.append((name, args))
        if self.gated:
            self.started.set()
            await self.release.wait()
            self.release.clear()
        return self.results.get(name, ToolResult(text="done"))

    async def cancel_running(self) -> None:
        self.cancels += 1

    def is_busy(self) -> bool:
        return False


def _bridge(toolset: ScriptedToolset, client: Any, sleeping: Sleeping | None = None) -> ToolBridge:
    return ToolBridge(client, cast(AgentToolSet, toolset), sleeping or Sleeping(), EventLog.open(None))


def _plan(*names_and_ids: tuple[str, str]) -> Response:
    return Response(
        id="r1",
        output=[{"type": "function_call", "name": n, "arguments": "{}", "call_id": c} for n, c in names_and_ids],
    )


def _sent_outputs(client: Any) -> list[tuple[str, str]]:
    """(call_id, output text) for every function_call_output client.send saw, in order."""
    return [
        (event.item["call_id"], event.item["output"])
        for event in client.sent
        if isinstance(event, ItemCreate) and event.item.get("type") == "function_call_output"
    ]


# ----------------------------------------------------------------------
# F1 -- epoch bump abandons the rest of a mid-flight plan
# ----------------------------------------------------------------------


async def test_epoch_bump_between_calls_flushes_the_remaining_plan(fake_client: type) -> None:
    client = fake_client()
    toolset = ScriptedToolset()
    toolset.gated = True
    bridge = _bridge(toolset, client)
    response = _plan(("nod", "c1"), ("wave_both", "c2"), ("bow", "c3"))

    task = asyncio.ensure_future(bridge._run_plan(response, bridge._epoch, is_idle=False))
    await toolset.started.wait()
    assert toolset.calls == [("nod", {})]

    await bridge.interrupt()  # bumps the epoch while c1 is still in flight
    toolset.release.set()  # let c1 finish normally (not an "interrupted:" result)
    await task

    assert toolset.calls == [("nod", {})], "a call after the epoch bump was executed"
    assert _sent_outputs(client) == [
        ("c1", "done"),
        ("c2", "interrupted: cancelled by barge-in"),
        ("c3", "interrupted: cancelled by barge-in"),
    ]


async def test_an_epoch_bump_before_the_task_ever_starts_aborts_the_whole_plan(fake_client: type) -> None:
    """The epoch is snapshotted synchronously in handle(), before the plan is even scheduled as a
    task -- so a barge-in that lands between handle() returning and the task's first turn on the
    event loop must abort the plan before it touches a single call, not race it."""
    client = fake_client()
    toolset = ScriptedToolset()
    bridge = _bridge(toolset, client)
    response = _plan(("nod", "c1"), ("wave_both", "c2"))

    await bridge.handle(response)  # snapshots epoch=0, schedules the plan task -- does not run it yet
    await bridge.interrupt()  # bumps the epoch to 1 before the scheduled task gets a turn
    pending = list(bridge.tasks)
    if pending:
        await asyncio.wait(pending)

    assert toolset.calls == [], "a call ran even though the epoch bumped before the task ever started"
    assert _sent_outputs(client) == [
        ("c1", "interrupted: cancelled by barge-in"),
        ("c2", "interrupted: cancelled by barge-in"),
    ]


async def test_a_barge_in_during_the_last_call_skips_the_speak_after_continuation(fake_client: type) -> None:
    """A barge-in landing after the last call already resolved cleanly must not start a NEW
    response that would compete with whatever the barge-in itself is about to trigger."""
    client = fake_client()
    toolset = ScriptedToolset()
    toolset.gated = True
    bridge = _bridge(toolset, client)
    response = _plan(("nod", "c1"))  # single call, no message item -> would normally get a continuation

    epoch = bridge._epoch
    task = asyncio.ensure_future(bridge._run_plan(response, epoch, is_idle=False))
    await toolset.started.wait()
    await bridge.interrupt()  # bumps the epoch while c1 is still in flight
    toolset.release.set()  # let c1 finish normally (not an "interrupted:" result)
    await task

    assert not any(isinstance(e, ResponseCreate) for e in client.sent), "a continuation was sent after a late barge-in"
    assert _sent_outputs(client) == [("c1", "done")]


async def test_an_interrupted_result_stops_the_plan_and_flushes_the_rest(fake_client: type) -> None:
    client = fake_client()
    toolset = ScriptedToolset(results={"nod": ToolResult(text="interrupted: cancelled mid-motion")})
    bridge = _bridge(toolset, client)
    response = _plan(("nod", "c1"), ("wave_both", "c2"))

    await bridge._run_plan(response, bridge._epoch, is_idle=False)

    assert toolset.calls == [("nod", {})], "a call after an interrupted result was still executed"
    assert _sent_outputs(client) == [
        ("c1", "interrupted: cancelled mid-motion"),
        ("c2", "interrupted: cancelled by barge-in"),
    ]


# ----------------------------------------------------------------------
# F3 -- a cancelled response executes nothing but still resolves call_ids
# ----------------------------------------------------------------------


async def test_a_cancelled_response_executes_nothing_but_resolves_every_call_id(fake_client: type) -> None:
    client = fake_client()
    toolset = ScriptedToolset()
    bridge = _bridge(toolset, client)
    response = Response(
        id="r1",
        status="cancelled",
        output=[
            {"type": "function_call", "name": "nod", "arguments": "{}", "call_id": "c1"},
            {"type": "function_call", "name": "wave_both", "arguments": "{}", "call_id": "c2"},
        ],
    )

    await bridge.handle(response)

    assert toolset.calls == [], "a cancelled response's calls were executed"
    assert _sent_outputs(client) == [
        ("c1", "interrupted: response cancelled by barge-in"),
        ("c2", "interrupted: response cancelled by barge-in"),
    ]


# ----------------------------------------------------------------------
# F2 -- idle-tagged responses run only the first call, no continuation
# ----------------------------------------------------------------------


async def test_idle_tagged_response_runs_only_the_first_call(fake_client: type) -> None:
    client = fake_client()
    toolset = ScriptedToolset()
    bridge = _bridge(toolset, client)
    response = Response(
        id="r1",
        metadata={"palmimo_turn": "idle"},
        output=[
            {"type": "function_call", "name": "nod", "arguments": "{}", "call_id": "c1"},
            {"type": "function_call", "name": "wave_both", "arguments": "{}", "call_id": "c2"},
        ],
    )

    await bridge._run_plan(response, bridge._epoch, is_idle=True)

    assert toolset.calls == [("nod", {})], "an idle tick ran more than one tool"
    assert not any(isinstance(e, ResponseCreate) for e in client.sent), "an idle tick asked for a spoken continuation"


async def test_idle_tick_flushes_the_skipped_tail_call_ids(fake_client: type) -> None:
    """The module docstring forbids a dangling call_id -- an idle tick's un-run calls must
    still get an answer, distinct from a barge-in interruption."""
    client = fake_client()
    toolset = ScriptedToolset()
    bridge = _bridge(toolset, client)
    response = Response(
        id="r1",
        metadata={"palmimo_turn": "idle"},
        output=[
            {"type": "function_call", "name": "nod", "arguments": "{}", "call_id": "c1"},
            {"type": "function_call", "name": "wave_both", "arguments": "{}", "call_id": "c2"},
            {"type": "function_call", "name": "bow", "arguments": "{}", "call_id": "c3"},
        ],
    )

    await bridge._run_plan(response, bridge._epoch, is_idle=True)

    assert toolset.calls == [("nod", {})]
    assert _sent_outputs(client) == [
        ("c2", "skipped: an idle tick does exactly one thing"),
        ("c3", "skipped: an idle tick does exactly one thing"),
        ("c1", "done"),
    ]


async def test_handle_routes_idle_metadata_through_the_single_call_path(fake_client: type) -> None:
    """handle() is the entry point BargeIn/EventRouter actually call -- exercise the metadata detection through it."""
    client = fake_client()
    toolset = ScriptedToolset()
    bridge = _bridge(toolset, client)
    response = Response(
        id="r1",
        metadata={"palmimo_turn": "idle"},
        output=[
            {"type": "function_call", "name": "nod", "arguments": "{}", "call_id": "c1"},
            {"type": "function_call", "name": "wave_both", "arguments": "{}", "call_id": "c2"},
        ],
    )

    await bridge.handle(response)
    pending = list(bridge.tasks)
    if pending:
        await asyncio.wait(pending)

    assert toolset.calls == [("nod", {})]


# ----------------------------------------------------------------------
# Sleeping -- only a clean result flips the mirror
# ----------------------------------------------------------------------


async def test_a_failed_sleep_call_does_not_flip_the_sleeping_mirror(fake_client: type) -> None:
    client = fake_client()
    toolset = ScriptedToolset(results={"sleep": ToolResult(text="the servo bus is not connected", is_error=True)})
    sleeping = Sleeping()
    bridge = _bridge(toolset, client, sleeping)
    response = _plan(("sleep", "c1"))

    await bridge._run_plan(response, bridge._epoch, is_idle=False)

    assert sleeping.asleep is False


async def test_an_interrupted_sleep_call_does_not_flip_the_sleeping_mirror(fake_client: type) -> None:
    client = fake_client()
    toolset = ScriptedToolset(results={"sleep": ToolResult(text="interrupted: cancelled by barge-in")})
    sleeping = Sleeping()
    bridge = _bridge(toolset, client, sleeping)
    response = _plan(("sleep", "c1"))

    await bridge._run_plan(response, bridge._epoch, is_idle=False)

    assert sleeping.asleep is False


async def test_a_successful_sleep_call_flips_the_sleeping_mirror(fake_client: type) -> None:
    client = fake_client()
    toolset = ScriptedToolset(results={"sleep": ToolResult(text="went to sleep")})
    sleeping = Sleeping()
    bridge = _bridge(toolset, client, sleeping)
    response = _plan(("sleep", "c1"))

    await bridge._run_plan(response, bridge._epoch, is_idle=False)

    assert sleeping.asleep is True


# ----------------------------------------------------------------------
# speak-after -- when does a plan earn a spoken continuation
# ----------------------------------------------------------------------


async def test_tool_calls_with_no_message_item_get_a_continuation(fake_client: type) -> None:
    client = fake_client()
    toolset = ScriptedToolset()
    bridge = _bridge(toolset, client)
    response = _plan(("nod", "c1"))  # no message item in output -> already_spoke is False

    await bridge._run_plan(response, bridge._epoch, is_idle=False)

    assert any(isinstance(e, ResponseCreate) for e in client.sent)


async def test_a_response_that_already_spoke_gets_no_continuation(fake_client: type) -> None:
    client = fake_client()
    toolset = ScriptedToolset()
    bridge = _bridge(toolset, client)
    response = Response(
        id="r1",
        output=[
            {"type": "message", "role": "assistant"},
            {"type": "function_call", "name": "nod", "arguments": "{}", "call_id": "c1"},
        ],
    )

    await bridge._run_plan(response, bridge._epoch, is_idle=False)

    assert not any(isinstance(e, ResponseCreate) for e in client.sent)


async def test_a_result_carrying_an_image_gets_a_continuation_even_if_already_spoken(fake_client: type) -> None:
    client = fake_client()
    toolset = ScriptedToolset(results={"capture": ToolResult(text="saw something", images=[b"\xff\xd8jpeg"])})
    bridge = _bridge(toolset, client)
    response = Response(
        id="r1",
        output=[
            {"type": "message", "role": "assistant"},
            {"type": "function_call", "name": "capture", "arguments": "{}", "call_id": "c1"},
        ],
    )

    await bridge._run_plan(response, bridge._epoch, is_idle=False)

    assert any(isinstance(e, ResponseCreate) for e in client.sent)
    image_items = [e for e in client.sent if isinstance(e, ItemCreate) and e.item.get("type") == "message"]
    assert len(image_items) == 1


async def test_a_plan_with_no_function_calls_is_a_no_op(fake_client: type) -> None:
    client = fake_client()
    toolset = ScriptedToolset()
    bridge = _bridge(toolset, client)
    response = Response(id="r1", output=[{"type": "message", "role": "assistant"}])

    await bridge._run_plan(response, bridge._epoch, is_idle=False)

    assert toolset.calls == []
    assert client.sent == []


async def test_bridge_logs_tool_arguments_as_readable_object(
    fake_client: type, capsys: pytest.CaptureFixture[str]
) -> None:
    bridge = _bridge(ScriptedToolset(), fake_client())
    response = Response(
        id="r1",
        output=[
            {
                "type": "function_call",
                "name": "nod",
                "call_id": "c1",
                "arguments": '{"reason":"\\u58c1\\u306e\\u82b1\\u67c4"}',
            }
        ],
    )
    await bridge.handle(response)
    await asyncio.gather(*bridge.tasks)
    output = capsys.readouterr().out
    assert output.splitlines()[0] == 'tool    > nod({"reason": "壁の花柄"})'


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param('{"reason": "unterminated', id="malformed"),
        pytest.param('{"value":NaN}', id="nan"),
    ],
)
async def test_bridge_preserves_tool_arguments_when_json_is_not_valid_object(
    arguments: str, fake_client: type, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _bridge(ScriptedToolset(), fake_client())
    monkeypatch.setattr(bridge.view, "call", AsyncMock(return_value=ToolResult(text="done")))
    response = Response(
        id="r1",
        output=[{"type": "function_call", "name": "nod", "arguments": arguments, "call_id": "c1"}],
    )
    await bridge.handle(response)
    await asyncio.gather(*bridge.tasks)
    assert capsys.readouterr().out.splitlines()[0] == f"tool    > nod({arguments})"


@pytest.mark.parametrize(
    ("arguments", "expected_args"),
    [
        pytest.param('{"reason": "\\ud83d"}', {"reason": "\ud83d"}, id="unpaired-surrogate"),
        pytest.param('{"v":' + "[" * 100_000 + "0" + "]" * 100_000 + "}", {}, id="deep-nesting"),
    ],
)
async def test_bridge_executes_and_answers_when_arguments_cannot_be_formatted(
    arguments: str, expected_args: dict[str, Any], fake_client: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = fake_client()
    bridge = _bridge(ScriptedToolset(), client)
    call = AsyncMock(return_value=ToolResult(text="done"))
    monkeypatch.setattr(bridge.view, "call", call)
    buffer = io.BytesIO()
    out = io.TextIOWrapper(buffer, encoding="utf-8", errors="strict")
    monkeypatch.setattr(sys, "stdout", out)
    response = Response(
        id="r1",
        output=[{"type": "function_call", "name": "nod", "arguments": arguments, "call_id": "c1"}],
    )
    await bridge.handle(response)
    await asyncio.gather(*bridge.tasks)
    call.assert_awaited_once_with("nod", expected_args)
    assert _sent_outputs(client) == [("c1", "done")]
    assert any(isinstance(event, ResponseCreate) for event in client.sent)
