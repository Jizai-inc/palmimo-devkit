"""Display vocabulary reaching companion prompts and deterministic actions."""

from collections.abc import Iterable
from typing import Any, Literal, cast

import pytest

from palmimo_companion_agent.core.perception import Detection, DetectionKind
from palmimo_companion_agent.core.reflexes import ReflexEngine
from palmimo_companion_agent.core.tools import Nod
from palmimo_companion_agent.pipeline.prompt import load_prompt as pipeline_prompt
from palmimo_companion_agent.realtime.prompts import load_prompt as realtime_prompt
from palmimo_sdk import FaceDisplay, Palmimo
from palmimo_sdk.agent import AgentToolSet


class Display:
    is_connected = True

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = entries
        self.calls: list[tuple[str, int | None]] = []

    def catalog(self, *, tags: Iterable[str] = (), locale: str | None = None) -> list[dict[str, Any]]:
        return self.entries

    def play(self, id: str, hold_ms: int | None = None) -> str:
        self.calls.append((id, hold_ms))
        return f"OK PLAY {id}"


@pytest.mark.parametrize("runtime", ["pipeline", "realtime"])
@pytest.mark.parametrize("kind", ["idle", "respond"])
def test_prompts_receive_catalog_vocabulary(runtime: str, kind: Literal["idle", "respond"]) -> None:
    entry: dict[str, Any] = {
        "id": "zip.example/unusual",
        "description": "Injected asset description",
        "params": {"color": {"description": "Injected parameter", "dim": 4}},
    }
    loader = pipeline_prompt if runtime == "pipeline" else realtime_prompt
    prompt = loader(kind, catalog=[entry])
    assert entry["id"] in prompt and entry["description"] in prompt
    assert entry["params"]["color"]["description"] in prompt
    assert "{{display_assets}}" not in prompt


@pytest.mark.parametrize("present", [False, True])
async def test_gesture_defaults_only_play_present_assets(present: bool) -> None:
    display = Display([{"id": "palmimo/maru", "description": "Agreement"}] if present else [])
    robot = Palmimo(display=cast(FaceDisplay, display))
    tools = AgentToolSet(robot, include=["nod"])
    tools.register(Nod)
    result = await tools.call("nod", {"seconds": 0.1})
    assert not result.is_error
    assert display.calls == ([("palmimo/maru", 100)] if present else [])


@pytest.mark.parametrize("present", [False, True])
async def test_name_reflex_only_reports_an_asset_it_actually_played(present: bool) -> None:
    display = Display([{"id": "palmimo/happy", "description": "Greeting"}] if present else [])
    tools = AgentToolSet(Palmimo(display=cast(FaceDisplay, display)))
    notes: list[str] = []
    engine = ReflexEngine(tools, notes.append)

    async def detections() -> Any:
        yield Detection(summary="called", kind=DetectionKind.NAME_CALL)

    await engine.run(detections())
    assert bool(display.calls) == present
    assert bool(notes) == present


@pytest.mark.parametrize("name", ["sleep", "wake_up"])
def test_explicit_asset_survives_sleep_and_wake_defaults(name: str) -> None:
    from palmimo_companion_agent.core.tools import Sleep, WakeUp
    from palmimo_sdk.agent import PalmimoLike

    display = Display(
        [
            {"id": id, "description": "Test"}
            for id in ("palmimo/custom", "palmimo/sleeping", "palmimo/sleepy", "palmimo/happy")
        ]
    )
    tools = AgentToolSet(Palmimo(display=cast(FaceDisplay, display)), include=[])
    tools.register(Sleep if name == "sleep" else WakeUp)

    class Robot:
        is_connected = True
        neck_lock_active = False
        neck_temperature_c = None

        def __init__(self) -> None:
            self.display = display

        def sleep(self) -> None:
            pass

        def wake(self) -> None:
            pass

        def stretch(self) -> None:
            pass

        def head_shake(self) -> None:
            pass

        def run(self, **kwargs: Any) -> None:
            pass

        def stop(self) -> None:
            pass

    tools.tool_models[name].model_validate({"face": "palmimo/custom"}).execute(cast(PalmimoLike, Robot()))
    assert display.calls == [("palmimo/custom", None)]
