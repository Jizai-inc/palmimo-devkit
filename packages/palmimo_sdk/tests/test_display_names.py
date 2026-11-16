"""Ownerless display names select owned assets consistently with catalog labels."""

import contextlib
from typing import Any

import pytest

from palmimo_sdk import FaceDisplay, FaceDisplayError, Palmimo
from palmimo_sdk.agent import AgentToolSet, render_display_prompt
from palmimo_sdk.agent.tools import SetFaceTool, ShowEmojiTool


class NamedDisplay(FaceDisplay):
    def __init__(self, app_id: str | None, shadow: bool = True) -> None:
        super().__init__()
        self._app_id = app_id
        self.commands: list[str] = []
        ids = ["palmimo/happy", "palmimo/sad", "palmimo/sleeping"]
        if app_id:
            ids += [f"{app_id}/sushi", f"{app_id}/smile", f"{app_id}/0-eye.color"]
            if shadow:
                ids += [f"{app_id}/happy"]
        self.inventory = [
            {
                "id": id,
                "description": "Meaning of " + id,
                "tags": ["app" if id.startswith(str(app_id) + "/") else "face"],
                "params": {},
                "translations": {},
            }
            for id in ids
        ]

    @property
    def is_connected(self) -> bool:
        return True

    def _check_caller(self) -> None:
        pass

    def _load_catalog(self) -> list[dict[str, Any]]:
        return self.inventory

    def _command(self, line: str, expected: str, *, allow_error: bool = False) -> str:
        self.commands.append(line)
        return expected


def skip_motion(*, seconds: float) -> None:
    pass


@pytest.mark.parametrize("entry", ["play", "play_asset", "face", "set_face", "show_emoji"])
@pytest.mark.parametrize("app_id", [None, "com.example.app"])
@pytest.mark.parametrize(
    "name",
    [
        "palmimo/happy",
        "com.example.app/sushi",
        "sushi",
        "sad",
        "happy",
        " HAPPY ",
        " PALMIMO/Happy ",
        " COM.EXAMPLE.APP/SUSHI ",
        "palmimo/happy\r",
        "smile",
        "laugh",
        "missing",
        "bad name",
    ],
)
async def test_display_entries_share_ownerless_resolution(
    monkeypatch: pytest.MonkeyPatch, entry: str, app_id: str | None, name: str
) -> None:
    if app_id:
        monkeypatch.setenv("PALMIMO_APP_ID", app_id)
    else:
        monkeypatch.delenv("PALMIMO_APP_ID", raising=False)
    display = NamedDisplay(app_id)
    robot = Palmimo(display=display)
    monkeypatch.setattr(robot, "run", skip_motion)
    normalized = name.strip().lower()
    expected = {
        "palmimo/happy": "palmimo/happy",
        "palmimo/happy ": "palmimo/happy",
        "sad": "palmimo/sad",
        "happy": f"{app_id}/happy" if app_id else "palmimo/happy",
    }.get(normalized)
    if app_id and normalized in {"sushi", "com.example.app/sushi", "smile"}:
        expected = f"{app_id}/" + ("smile" if normalized == "smile" else "sushi")
    if entry in {"face", "set_face", "show_emoji"} and normalized in {"laugh", "smile"} and expected is None:
        expected = "palmimo/happy"
    hold = 3000 if entry == "show_emoji" else None
    with pytest.warns(DeprecationWarning) if entry in {"set_face", "show_emoji"} else contextlib.nullcontext():
        if entry == "play":
            if expected is None:
                with pytest.raises((ValueError, FaceDisplayError)):
                    display.play(name)
            else:
                assert display.play(name) == f"OK PLAY {expected}"
        elif entry in {"set_face", "show_emoji"}:
            cls = SetFaceTool if entry == "set_face" else ShowEmojiTool
            result = cls.model_validate({"name": name}).execute(robot)
            assert result.is_error == (expected is None)
        else:
            tools = AgentToolSet(robot, include=["forward" if entry == "face" else "play_asset"])
            args = {"face": name, "seconds": 0.5} if entry == "face" else {"id": name}
            result = await tools.call(tools.tool_names[0], args)
            assert result.is_error == (expected is None)
    assert display.commands == ([f"PLAY {expected}" + (f" {hold}" if hold else "")] if expected else [])


@pytest.mark.parametrize(
    "app_id,shadow",
    [
        (None, False),
        ("com.example.app", False),
        ("com.example.app", True),
        ("owner.my-app", True),
        ("zip.my-app-2", True),
    ],
)
def test_catalog_names_round_trip_to_their_full_ids(
    monkeypatch: pytest.MonkeyPatch, app_id: str | None, shadow: bool
) -> None:
    if app_id:
        monkeypatch.setenv("PALMIMO_APP_ID", app_id)
    else:
        monkeypatch.delenv("PALMIMO_APP_ID", raising=False)
    display = NamedDisplay(app_id, shadow)
    catalog = display.catalog()
    for asset in catalog:
        expected_name = (
            "palmimo/happy" if asset["id"] == "palmimo/happy" and app_id and shadow else asset["id"].split("/", 1)[1]
        )
        assert asset["name"] == expected_name
        assert display.play(asset["name"]) == f"OK PLAY {asset['id']}"
    assert display.commands == [f"PLAY {asset['id']}" for asset in catalog]


@pytest.mark.parametrize("entry", ["play_asset", "forward"])
async def test_filtered_tool_names_preserve_shadowing_and_accept_full_ids(
    monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    monkeypatch.setenv("PALMIMO_APP_ID", "com.example.app")
    display = NamedDisplay("com.example.app")
    robot = Palmimo(display=display)
    monkeypatch.setattr(robot, "run", skip_motion)
    tools = AgentToolSet(robot, include=["play_asset", "forward"], display_tags=["face"])
    catalog = tools.display_catalog
    names = [asset["name"] for asset in catalog]
    assert names == ["palmimo/happy", "sad", "sleeping"]
    schemas = {name: cls.parameters_schema() for name, cls in tools.tool_models.items()}
    assert schemas["play_asset"]["properties"]["id"]["enum"] == names
    assert schemas["forward"]["properties"]["face"]["anyOf"][0]["enum"] == names
    prompt = render_display_prompt("{{display_assets}}", catalog)
    assert "- sad:" in prompt and "- palmimo/sad:" not in prompt

    def arguments(value: str) -> dict[str, Any]:
        return {"id": value} if entry == "play_asset" else {"face": value, "seconds": 0.5}

    for value in ("happy", "smile"):
        assert (await tools.call(entry, arguments(value))).is_error
    for value in ("sad", "PALMIMO/SAD"):
        assert not (await tools.call(entry, arguments(value))).is_error
    assert display.commands == ["PLAY palmimo/sad", "PLAY palmimo/sad"]


@pytest.mark.parametrize("cls", [SetFaceTool, ShowEmojiTool])
def test_legacy_tools_match_modern_missing_display_errors(cls: type[SetFaceTool | ShowEmojiTool]) -> None:
    from palmimo_sdk.agent.tools import PlayAssetTool

    robot = Palmimo()
    with pytest.warns(DeprecationWarning):
        legacy = cls.model_validate({"name": "happy"}).execute(robot)
    modern = PlayAssetTool(id="happy").execute(robot)
    assert legacy.is_error == modern.is_error is True


@pytest.mark.parametrize("values", [[1, 0, 0, 1], [1]])
async def test_short_names_validate_parameters_for_the_resolved_asset(
    monkeypatch: pytest.MonkeyPatch, values: list[int]
) -> None:
    monkeypatch.setenv("PALMIMO_APP_ID", "com.example.app")
    display = NamedDisplay("com.example.app")
    for asset in display.inventory:
        if asset["id"] == "com.example.app/sushi":
            asset["params"] = {"tint": {"description": "Color", "dim": 4}}
    tools = AgentToolSet(Palmimo(display=display), include=["play_asset"])
    result = await tools.call("play_asset", {"id": "SUSHI", "params": {"tint": values}})
    assert result.is_error == (len(values) != 4)
    if len(values) == 4:
        assert display.commands[0] == "PLAY com.example.app/sushi"
        assert display.commands[1].startswith("PARAM tint ")
    else:
        assert display.commands == []
