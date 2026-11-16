"""Tests for :mod:`palmimo_companion_agent.core.toolview`."""

from __future__ import annotations

from palmimo_companion_agent.core.toolview import IDLE_TOOL_NAMES, RESPOND_ONLY_TOOL_NAMES, ToolView
from palmimo_sdk.agent.toolset import AgentToolSet


def test_idle_and_respond_only_tool_names_exactly_partition_the_registered_tools(toolset: AgentToolSet) -> None:
    """Guards against silent drift: a new tool the SDK or this agent registers must be
    explicitly classified into one of these two sets, or this fails loudly instead of the
    tool silently falling through to whichever view happens to see it."""
    registered = set(toolset.tool_names)
    assert set() == IDLE_TOOL_NAMES & RESPOND_ONLY_TOOL_NAMES  # disjoint
    assert registered == IDLE_TOOL_NAMES | RESPOND_ONLY_TOOL_NAMES  # exhaustive


def test_to_openai_tools_returns_everything_when_allow_is_none(toolset: AgentToolSet) -> None:
    view = ToolView(toolset)
    names = {tool["function"]["name"] for tool in view.to_openai_tools()}
    assert names == set(toolset.tool_names)


def test_to_openai_tools_filters_by_allowlist(toolset: AgentToolSet) -> None:
    view = ToolView(toolset, allow=frozenset({"nod", "rest"}))
    names = {tool["function"]["name"] for tool in view.to_openai_tools()}
    assert names == {"nod", "rest"}


def test_to_openai_tools_strips_the_say_property_when_squash_say(toolset: AgentToolSet) -> None:
    view = ToolView(toolset, allow=frozenset({"nod"}), squash_say=True)
    [tool] = view.to_openai_tools()
    properties = tool["function"]["parameters"]["properties"]
    assert "say" not in properties
    required = tool["function"]["parameters"].get("required", [])
    assert "say" not in required
    # Every other property (e.g. seconds) must survive the strip.
    assert "seconds" in properties


def test_to_openai_tools_keeps_the_say_property_without_squash_say(toolset: AgentToolSet) -> None:
    view = ToolView(toolset, allow=frozenset({"nod"}), squash_say=False)
    [tool] = view.to_openai_tools()
    assert "say" in tool["function"]["parameters"]["properties"]


def test_to_openai_tools_squash_say_never_mutates_the_underlying_toolset_schema(toolset: AgentToolSet) -> None:
    squashed_view = ToolView(toolset, allow=frozenset({"nod"}), squash_say=True)
    squashed_view.to_openai_tools()
    plain_view = ToolView(toolset, allow=frozenset({"nod"}))
    [tool] = plain_view.to_openai_tools()
    assert "say" in tool["function"]["parameters"]["properties"]


def test_is_long_running_delegates_to_the_underlying_toolset(toolset: AgentToolSet) -> None:
    view = ToolView(toolset)
    assert view.is_long_running("dance") is True
    assert view.is_long_running("set_face") is False


def test_is_long_running_is_false_for_an_unknown_tool(toolset: AgentToolSet) -> None:
    view = ToolView(toolset)
    assert view.is_long_running("no_such_tool") is False


async def test_call_of_a_disallowed_tool_is_a_non_raising_error_result(toolset: AgentToolSet) -> None:
    view = ToolView(toolset, allow=frozenset({"nod"}))
    result = await view.call("dance", {"seconds": 1.0, "reason": "test"})
    assert result.is_error is True
    assert "not available" in result.text


async def test_call_of_an_allowed_tool_dispatches_normally(toolset: AgentToolSet) -> None:
    view = ToolView(toolset, allow=frozenset({"rest"}))
    result = await view.call("rest", {"seconds": 0.01, "reason": "test"})
    assert result.is_error is False
    assert "rested" in result.text


async def test_squash_say_removes_the_say_argument_before_dispatch(toolset: AgentToolSet) -> None:
    view = ToolView(toolset, allow=frozenset({"nod"}), squash_say=True)
    result = await view.call("nod", {"seconds": 0.5, "say": "hello", "reason": "test"})
    # The bare (say=None) SDK NodTool never speaks, so "said:"/"speaking" notes
    # would only show up if `say` reached the underlying call.
    assert result.is_error is False
    assert "said" not in result.text
    assert "speaking" not in result.text


async def test_without_squash_say_the_say_argument_reaches_the_call(toolset: AgentToolSet) -> None:
    view = ToolView(toolset, allow=frozenset({"nod"}), squash_say=False)
    result = await view.call("nod", {"seconds": 0.5, "say": "hello", "reason": "test"})
    assert "no speaker attached" in result.text


def test_is_busy_and_cancel_running_are_passthroughs(toolset: AgentToolSet) -> None:
    view = ToolView(toolset)
    assert view.is_busy() == toolset.is_busy()


async def test_cancel_running_delegates_to_the_underlying_toolset(toolset: AgentToolSet) -> None:
    calls = 0

    async def fake_cancel_running() -> None:
        nonlocal calls
        calls += 1

    toolset.cancel_running = fake_cancel_running  # type: ignore[method-assign]
    view = ToolView(toolset)
    await view.cancel_running()
    assert calls == 1
