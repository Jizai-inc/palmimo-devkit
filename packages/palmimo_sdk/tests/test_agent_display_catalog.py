"""Connected display vocabulary at the agent and MCP boundaries."""

import json
from typing import Any, cast

import pytest

from palmimo_sdk import Palmimo
from palmimo_sdk.agent import AgentToolSet
from palmimo_sdk.agent.tools import WaveTool

from .conftest import CatalogDisplay, load_display_assets


def make_tools(display: CatalogDisplay | None, **kwargs: Any) -> AgentToolSet:
    from palmimo_sdk import FaceDisplay

    return AgentToolSet(Palmimo(display=cast(FaceDisplay | None, display)), **kwargs)


@pytest.mark.parametrize("locale", [None, "ja-jp"])
def test_agent_schema_uses_catalog_names_and_requested_language(locale: str | None) -> None:
    """Without this, tool vocabulary can diverge from the selected catalog and locale."""
    display = CatalogDisplay()
    tools = make_tools(display, display_tags=["face"], display_locale=locale)
    schemas = {t["function"]["name"]: t["function"] for t in tools.to_openai_tools()}
    names = [a["name"] for a in tools.display_catalog]
    assert schemas["play_asset"]["parameters"]["properties"]["id"]["enum"] == names
    assert schemas["forward"]["parameters"]["properties"]["face"]["anyOf"][0]["type"] == "string"
    assert display.queries == [((), locale)]


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


async def test_unknown_asset_is_rejected_before_tool_execution() -> None:
    display = CatalogDisplay()
    result = await make_tools(display).call("play_asset", {"id": "palmimo/unknown"})
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


def test_catalog_growth_does_not_repeat_asset_meanings_in_tool_schemas() -> None:
    """Asset choices may grow without repeating their meanings in every tool."""
    assets = load_display_assets()
    tools = make_tools(CatalogDisplay(assets))
    schemas = json.dumps(tools.to_openai_tools())
    for asset in assets:
        assert asset["description"] not in schemas
    choices = tools.tool_models["play_asset"].parameters_schema()["properties"]["id"]["enum"]
    assert choices == [asset["name"] for asset in tools.display_catalog]


@pytest.mark.parametrize("state", ["connected", "empty", "disconnected", "missing"])
@pytest.mark.parametrize("face", ["unknown", "palmimo/unknown", "other.app/happy", "bad/name/extra", "bad name"])
async def test_unknown_face_does_not_prevent_motion(monkeypatch: pytest.MonkeyPatch, face: str, state: str) -> None:
    display = CatalogDisplay([] if state == "empty" else None)
    display.is_connected = state != "disconnected"
    robot = Palmimo(display=cast(Any, display) if state != "missing" else None)
    actions: list[str] = []
    monkeypatch.setattr(robot, "wave", lambda **kwargs: actions.append("wave"))
    monkeypatch.setattr(robot, "run", lambda **kwargs: None)
    tools = AgentToolSet(robot, include=["wave"])
    result = await tools.call("wave", {"face": face, "seconds": 0.5})
    assert not result.is_error
    assert actions == ["wave"]
    assert not display.calls
    assert "not shown" in result.text
