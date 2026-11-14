"""Connected display vocabulary at the agent and MCP boundaries."""

import copy
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.client import Client

from palmimo_sdk import Palmimo
from palmimo_sdk.agent import AgentToolSet
from palmimo_sdk.agent.tools import WaveTool
from palmimo_sdk.mcp import build_mcp_server


class CatalogDisplay:
    def __init__(self, entries: list[dict[str, Any]] | None = None) -> None:
        self.is_connected = True
        inventory = json.loads((Path(__file__).parents[3] / "tests/fixtures/display-assets/catalog.json").read_text())
        self.entries = entries if entries is not None else inventory["assets"][:2]
        self.queries: list[tuple[tuple[str, ...], str | None]] = []
        self.calls: list[tuple[str, int | None]] = []
        self.failure: Exception | None = None

    def catalog(self, *, tags: Iterable[str] = (), locale: str | None = None) -> list[dict[str, Any]]:
        self.queries.append((tuple(tags), locale))
        if self.failure:
            raise self.failure
        return copy.deepcopy(self.entries)

    def play(self, id: str, hold_ms: int | None = None) -> str:
        self.calls.append((id, hold_ms))
        return f"OK PLAY {id}"


def make_tools(display: CatalogDisplay | None, **kwargs: Any) -> AgentToolSet:
    from palmimo_sdk import FaceDisplay

    return AgentToolSet(Palmimo(display=cast(FaceDisplay | None, display)), **kwargs)


@pytest.mark.parametrize("locale", [None, "ja-jp"])
def test_agent_schema_uses_catalog_ids_and_requested_language(locale: str | None) -> None:
    """Without this, tool vocabulary can diverge from the selected catalog and locale."""
    display = CatalogDisplay()
    display.entries[0]["description"] = "Injected description " + str(locale)
    tools = make_tools(display, display_tags=["face"], display_locale=locale)
    schemas = {t["function"]["name"]: t["function"] for t in tools.to_openai_tools()}
    ids = [a["id"] for a in display.entries]
    assert schemas["play_asset"]["parameters"]["properties"]["id"]["enum"] == ids
    assert schemas["forward"]["parameters"]["properties"]["face"]["anyOf"][0]["type"] == "string"
    assert display.queries == [(("face",), locale)]


@pytest.mark.parametrize("state", ["missing", "empty", "disconnected"])
def test_agent_hides_display_tools_and_face_arguments_without_assets(state: str) -> None:
    display = None if state == "missing" else CatalogDisplay([])
    if display is not None:
        display.is_connected = state != "disconnected"
    tools = make_tools(display)
    schemas = {t["function"]["name"]: t["function"] for t in tools.to_openai_tools()}
    assert "play_asset" not in schemas
    assert "face" not in schemas["forward"]["parameters"]["properties"]
    assert "set_face" not in schemas and "show_emoji" not in schemas


@pytest.mark.parametrize(
    "name,args", [("play_asset", {"id": "palmimo/unknown"}), ("forward", {"seconds": 0.01, "face": "palmimo/unknown"})]
)
async def test_unknown_asset_is_rejected_before_tool_execution(name: str, args: dict[str, Any]) -> None:
    display = CatalogDisplay()
    tools = make_tools(display)
    result = await tools.call(name, args)
    assert result.is_error
    assert display.calls == []


@pytest.mark.parametrize("hold_ms", [None, 1500])
async def test_asset_tool_plays_catalog_id_with_optional_hold(hold_ms: int | None) -> None:
    display = CatalogDisplay()
    tools = make_tools(display)
    id = display.entries[0]["id"]
    result = await tools.call("play_asset", {"id": id, "hold_ms": hold_ms})
    assert not result.is_error
    assert display.calls == [(id, hold_ms)]


def test_registered_expressive_tools_are_bound_per_toolset() -> None:
    first, second = CatalogDisplay(), CatalogDisplay([])
    first_tools, second_tools = make_tools(first), make_tools(second)
    first_tools.register(WaveTool)
    second_tools.register(WaveTool)
    first_schema = first_tools.tool_models["wave"].parameters_schema()
    second_schema = second_tools.tool_models["wave"].parameters_schema()
    assert "face" in first_schema["properties"]
    assert "face" not in second_schema["properties"]
    assert "enum" not in json.dumps(WaveTool.parameters_schema())


def test_refresh_replaces_vocabulary_and_clears_stale_choices_on_failure() -> None:
    display = CatalogDisplay()
    tools = make_tools(display)
    display.entries = []
    tools.refresh_display()
    assert tools.display_catalog == [] and "play_asset" not in tools.tool_names
    display.entries = [{"id": "zip.app/new", "description": "New", "tags": [], "params": {}}]
    tools.refresh_display()
    assert tools.display_catalog[0]["id"] == "zip.app/new"
    display.failure = OSError("catalog unavailable")
    with pytest.raises(OSError):
        tools.refresh_display()
    assert tools.display_catalog == [] and "play_asset" not in tools.tool_names
    display.failure = None
    tools.register(WaveTool)
    live_view = tools.tool_models
    tools.refresh_display()
    assert "wave" in live_view and "play_asset" in live_view
    snapshot = tools.display_catalog
    snapshot.clear()
    assert tools.display_catalog[0]["id"] == "zip.app/new"


async def test_mcp_exposes_and_dispatches_the_same_catalog_vocabulary() -> None:
    display = CatalogDisplay()
    tools = make_tools(display)
    expected = {t["function"]["name"]: t["function"] for t in tools.to_openai_tools()}
    async with Client(build_mcp_server(tools)) as client:
        listed = await client.list_tools()
        for tool in listed.tools:
            assert tool.input_schema == expected[tool.name]["parameters"]
            assert tool.description == expected[tool.name]["description"]
        result = await client.call_tool("play_asset", {"id": display.entries[0]["id"]})
    assert not result.is_error
    assert display.calls == [(display.entries[0]["id"], None)]


@pytest.mark.parametrize("reply", ["OK PARAM color", "ERR PARAM NOSLOT"])
async def test_asset_parameters_follow_play_and_missing_slots_are_nonfatal(reply: str) -> None:
    class ParameterDisplay(CatalogDisplay):
        def param(self, sid: str, *values: float) -> str:
            assert self.calls == [("zip.app/custom", None)]
            self.parameters = (sid, values)
            return reply

    display = ParameterDisplay(
        [
            {
                "id": "zip.app/custom",
                "description": "Custom",
                "params": {"color": {"description": "Injected color description", "dim": 4}},
            }
        ]
    )
    tools = make_tools(display)
    schema = tools.tool_models["play_asset"].parameters_schema()
    assert "Injected color description" in json.dumps(schema)
    result = await tools.call("play_asset", {"id": "zip.app/custom", "params": {"color": [1, 0, 0, 1]}})
    assert not result.is_error
    assert display.parameters == ("color", (1, 0, 0, 1))


@pytest.mark.parametrize("params", [{"color": [1]}, {"unknown": [1, 0, 0, 1]}, {"color": [float("nan"), 0, 0, 1]}])
async def test_invalid_asset_parameters_are_rejected_before_play(params: dict[str, list[float]]) -> None:
    display = CatalogDisplay(
        [{"id": "zip.app/custom", "description": "Custom", "params": {"color": {"description": "Color", "dim": 4}}}]
    )
    result = await make_tools(display).call("play_asset", {"id": "zip.app/custom", "params": params})
    assert result.is_error and display.calls == []


@pytest.mark.parametrize("reply", ["ERR PLAY UNKNOWN", "", "   "])
async def test_failed_play_never_changes_previous_asset_parameters(reply: str) -> None:
    class FailedDisplay(CatalogDisplay):
        parameter_changed = False

        def play(self, id: str, hold_ms: int | None = None) -> str:
            return reply

        def param(self, sid: str, *values: float) -> str:
            self.parameter_changed = True
            return "OK PARAM color"

    display = FailedDisplay(
        [{"id": "zip.app/custom", "description": "Custom", "params": {"color": {"description": "Color", "dim": 1}}}]
    )
    result = await make_tools(display).call("play_asset", {"id": "zip.app/custom", "params": {"color": [1]}})
    assert result.is_error
    assert not display.parameter_changed


def test_hidden_display_registration_replaces_existing_tool() -> None:
    from palmimo_sdk.agent.tools import PlayAssetTool

    class DisplayOnlyForward(PlayAssetTool):
        name = "forward"

    tools = make_tools(None)
    tools.register(DisplayOnlyForward)
    assert "forward" not in tools.tool_names


@pytest.mark.parametrize("operation", ["refresh", "register"])
async def test_vocabulary_mutation_is_rejected_during_a_tool_call(operation: str) -> None:
    import asyncio
    import threading
    from typing import ClassVar

    from palmimo_sdk.agent import PalmimoLike
    from palmimo_sdk.agent.tools import Tool, ToolResult

    entered, release = threading.Event(), threading.Event()

    class WaitTool(Tool):
        name: ClassVar[str] = "wait"
        description: ClassVar[str] = "Wait for the test caller."

        def execute(self, robot: PalmimoLike) -> ToolResult:
            entered.set()
            release.wait(2)
            return ToolResult(text="done")

    tools = make_tools(CatalogDisplay())
    tools.register(WaitTool)
    task = asyncio.create_task(tools.call("wait", {}))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(RuntimeError):
            if operation == "refresh":
                tools.refresh_display()
            else:
                tools.register(WaveTool)
    finally:
        release.set()
        assert not (await task).is_error


def test_catalog_growth_does_not_multiply_tool_schema_cost() -> None:
    """Without this, every motion tool repeats the entire growing asset catalog."""
    inventory = json.loads((Path(__file__).parents[3] / "tests/fixtures/display-assets/catalog.json").read_text())
    small = json.dumps(make_tools(CatalogDisplay()).to_openai_tools())
    large = json.dumps(make_tools(CatalogDisplay(inventory["assets"])).to_openai_tools())
    assert len(large) - len(small) < 8000
