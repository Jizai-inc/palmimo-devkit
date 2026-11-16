"""Deprecated display entry points resolve names to the same protocol 2 assets."""

import contextlib
import json
from typing import Any

import pytest

from palmimo_sdk import FaceDisplay, FaceDisplayError, Palmimo
from palmimo_sdk.agent import AgentToolSet
from palmimo_sdk.agent import tools as agent_tools


class PlaybackDisplay(FaceDisplay):
    def __init__(self) -> None:
        super().__init__()
        self._app_id = "com.example.app"
        self.commands: list[str] = []

    @property
    def is_connected(self) -> bool:
        return True

    def catalog(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"id": id, "description": "Test asset", "params": {}}
            for id in (
                "palmimo/happy",
                "com.example.app/happy",
                "com.example.app/sushi",
            )
        ]

    def _command(self, line: str, expected: str, *, allow_error: bool = False) -> str:
        self.commands.append(line)
        return expected


@pytest.mark.parametrize("entry", ["display", "robot", "set_face", "face"])
@pytest.mark.parametrize(
    "name,expected",
    [
        ("palmimo/happy", "palmimo/happy"),
        ("com.example.app/sushi", "com.example.app/sushi"),
        (" Happy ", "com.example.app/happy"),
        ("hApPy", "com.example.app/happy"),
        ("SUSHI", "com.example.app/sushi"),
        ("missing", None),
        ("other.app/sushi", None),
    ],
)
async def test_legacy_names_resolve_consistently(
    monkeypatch: pytest.MonkeyPatch, entry: str, name: str, expected: str | None
) -> None:
    monkeypatch.setenv("PALMIMO_APP_ID", "com.example.app")
    display = PlaybackDisplay()
    robot = Palmimo(display=display)
    if entry in {"display", "robot"}:
        with pytest.warns(DeprecationWarning):
            if expected is None:
                with pytest.raises((FaceDisplayError, ValueError)):
                    (display if entry == "display" else robot).set_expression(name)
            else:
                (display if entry == "display" else robot).set_expression(name)
    else:
        include = ["set_face"] if entry == "set_face" else ["forward"]
        toolset = AgentToolSet(robot, include=include)
        args = {"name": name} if entry == "set_face" else {"face": name, "seconds": 0.5}
        with pytest.warns(DeprecationWarning) if entry == "set_face" else contextlib.nullcontext():
            result = await toolset.call(include[0], args)
        assert result.is_error == (expected is None)
    assert display.commands == ([f"PLAY {expected}"] if expected else [])


@pytest.mark.parametrize("name,seconds,hold", [("happy", 3.0, 3000), ("SUSHI", 0.5, 500), ("missing", 10.0, None)])
def test_show_emoji_delegates_timed_playback(
    monkeypatch: pytest.MonkeyPatch, name: str, seconds: float, hold: int | None
) -> None:
    monkeypatch.setenv("PALMIMO_APP_ID", "com.example.app")
    display = PlaybackDisplay()
    tool = agent_tools.ShowEmojiTool.model_validate({"name": name, "seconds": seconds})
    with pytest.warns(DeprecationWarning):
        result = tool.execute(Palmimo(display=display))
    assert result.is_error == (hold is None)
    expected = "com.example.app/happy" if name == "happy" else "com.example.app/sushi"
    assert display.commands == ([f"PLAY {expected} {hold}"] if hold else [])


def test_legacy_tools_are_opt_in_with_original_argument_shapes() -> None:
    robot = Palmimo(display=PlaybackDisplay())
    assert not {"set_face", "show_emoji"} & set(AgentToolSet(robot).tool_names)
    tools = AgentToolSet(robot, include=["set_face", "show_emoji"])
    assert set(tools.tool_names) == {"set_face", "show_emoji"}
    for name, fields in [("set_face", {"name", "reason"}), ("show_emoji", {"name", "reason", "seconds"})]:
        schema = tools.tool_models[name].parameters_schema()
        assert set(schema["properties"]) == fields
        assert schema["required"] == ["name"]
        assert schema["properties"]["name"]["type"] == "string"
    seconds = tools.tool_models["show_emoji"].parameters_schema()["properties"]["seconds"]
    assert {key: seconds[key] for key in ("default", "minimum", "maximum")} == {
        "default": 3.0,
        "minimum": 0.5,
        "maximum": 10.0,
    }
    assert "enum" not in json.dumps(tools.tool_models["set_face"].parameters_schema())


@pytest.mark.parametrize("cls", [agent_tools.SetFaceTool, agent_tools.ShowEmojiTool])
@pytest.mark.parametrize("failure", ["unknown", "playback"])
def test_legacy_tools_return_error_results_without_raising(
    cls: type[agent_tools.Tool], failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    display = PlaybackDisplay()
    if failure == "playback":

        def reject(id: str, hold_ms: int | None = None) -> str:
            raise FaceDisplayError("Playback unavailable")

        monkeypatch.setattr(display, "play", reject)
    tool = cls.model_validate({"name": "missing" if failure == "unknown" else "happy"})
    with pytest.warns(DeprecationWarning):
        result = tool.execute(Palmimo(display=display))
    assert result.is_error
    assert display.commands == []


def test_facade_warns_when_deprecated_display_call_has_no_display() -> None:
    with pytest.warns(DeprecationWarning):
        assert Palmimo().set_expression("HAPPY") is None
