"""Behavior of `palmimo_controller.ai.tools.run_tool`: tool calls become session intents."""

import json

import pytest

from palmimo_controller.ai.tools import TOOL_DEFINITIONS, run_tool
from palmimo_controller.session import (
    AI_START_NOD_SECONDS,
    AI_TOOL_MAX_SECONDS,
    GESTURE_PLAY_SECONDS,
    PilotInput,
    PilotSession,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _ai_session() -> tuple[PilotSession, FakeClock]:
    clock = FakeClock()
    session = PilotSession(now=clock)
    session.acquire_pilot("a")
    session.begin_ai("a", "default")
    session.ai_started()
    # Let the go-live nod finish, so each test sees only its own tool call.
    clock.now = AI_START_NOD_SECONDS
    return session, clock


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("walk", {"direction": "forward", "seconds": 2}, PilotInput(move="forward")),
        ("walk", {"direction": "left", "seconds": 2}, PilotInput(move="strafe_left")),
        ("turn", {"direction": "right", "seconds": 1}, PilotInput(rotate=1)),
        ("gesture", {"name": "wave"}, PilotInput(gesture="wave")),
        ("gesture", {"name": "stretch"}, PilotInput(gesture="stretch")),
        ("gesture", {"name": "wave_both"}, PilotInput(gesture="wave_both")),
        ("gesture", {"name": "head_shake"}, PilotInput(gesture="head_shake")),
        ("look", {"pitch": 0.5, "yaw": -2}, PilotInput(neck_pitch=0.5, neck_yaw=-1.0)),
        ("stop", {}, PilotInput()),
    ],
)
def test_run_tool_sets_the_matching_intent(name: str, arguments: dict, expected: PilotInput) -> None:
    session, _ = _ai_session()
    assert json.loads(run_tool(session, name, json.dumps(arguments))) == {"ok": True}
    assert session.effective_input() == expected


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("fly", "{}"),
        ("walk", '{"direction": "up", "seconds": 1}'),
        ("walk", '{"direction": "forward", "seconds": "long"}'),
        ("look", '{"pitch": true, "yaw": 0}'),
        ("walk", "not json"),
        ("walk", "[]"),
        ("gesture", '{"name": "moonwalk"}'),
        ("gesture", '{"name": ["wave"]}'),
        ("set_face", '{"expression": "SMIRK", "seconds": 1}'),
        ("set_face", '{"expression": 3, "seconds": 1}'),
        ("set_face", '{"expression": "HAPPY", "seconds": "long"}'),
        ("set_face", '{"seconds": 1}'),
    ],
)
def test_run_tool_reports_a_bad_call_to_the_model(name: str, arguments: str) -> None:
    # Without this, one malformed call from the model would crash the AI session.
    session, _ = _ai_session()
    result = json.loads(run_tool(session, name, arguments))
    assert result["ok"] is False
    assert session.effective_input() == PilotInput()


def test_run_tool_refuses_when_ai_mode_has_ended() -> None:
    session, _ = _ai_session()
    session.end_ai()
    assert json.loads(run_tool(session, "walk", '{"direction": "forward", "seconds": 1}'))["ok"] is False


def test_every_tool_definition_is_runnable() -> None:
    names = {definition["name"] for definition in TOOL_DEFINITIONS}
    assert names == {"walk", "turn", "gesture", "look", "set_face", "stop"}


def test_set_face_shows_the_expression_without_moving() -> None:
    session, _ = _ai_session()
    run_tool(session, "gesture", '{"name": "bow"}')
    assert json.loads(run_tool(session, "set_face", '{"expression": "smile", "seconds": 2}')) == {"ok": True}
    assert session.face_cue().expression == "SMILE"
    # The face must not cut off the bow that is playing.
    assert session.effective_input() == PilotInput(gesture="bow")


def test_set_face_refuses_when_ai_mode_has_ended() -> None:
    session, _ = _ai_session()
    session.end_ai()
    assert json.loads(run_tool(session, "set_face", '{"expression": "HAPPY", "seconds": 1}'))["ok"] is False


def test_gesture_enum_offers_every_timed_gesture() -> None:
    by_name = {definition["name"]: definition for definition in TOOL_DEFINITIONS}
    assert set(by_name["gesture"]["parameters"]["properties"]["name"]["enum"]) == set(GESTURE_PLAY_SECONDS)


@pytest.mark.parametrize(
    ("name", "arguments", "cap"),
    [
        ("walk", {"direction": "forward", "seconds": 60}, AI_TOOL_MAX_SECONDS["walk"]),
        ("turn", {"direction": "left", "seconds": 60}, AI_TOOL_MAX_SECONDS["turn"]),
        ("look", {"pitch": 0.3, "yaw": 0.3}, AI_TOOL_MAX_SECONDS["look"]),
        ("gesture", {"name": "bow"}, GESTURE_PLAY_SECONDS["bow"]),
        ("gesture", {"name": "dance"}, GESTURE_PLAY_SECONDS["dance"]),
    ],
)
def test_each_tool_intent_ends_at_its_own_cap(name: str, arguments: dict, cap: float) -> None:
    # The schema maximum is only advice to the model, so the cap must hold on its own.
    session, clock = _ai_session()
    start = clock.now
    run_tool(session, name, json.dumps(arguments))
    clock.now = start + cap - 0.01
    assert session.effective_input() != PilotInput()
    clock.now = start + cap
    assert session.effective_input() == PilotInput()


def test_seconds_schema_maximum_matches_each_tool_cap() -> None:
    by_name = {definition["name"]: definition for definition in TOOL_DEFINITIONS}
    for tool in ("walk", "turn"):
        seconds = by_name[tool]["parameters"]["properties"]["seconds"]
        assert seconds["maximum"] == AI_TOOL_MAX_SECONDS[tool]
        assert f"{AI_TOOL_MAX_SECONDS[tool]:g}" in seconds["description"]


def test_run_tool_logs_each_call_with_what_was_applied() -> None:
    from palmimo_controller.telemetry import Telemetry

    session, _ = _ai_session()
    events: list[dict] = []
    telemetry = Telemetry(emit=lambda line: events.append(json.loads(line)))
    run_tool(session, "turn", '{"direction": "left", "seconds": 1.5}', telemetry=telemetry)
    run_tool(session, "gesture", '{"name": "moonwalk"}', telemetry=telemetry)
    ok, bad = events
    assert (ok["type"], ok["name"], ok["ok"], ok["applied_seconds"]) == ("tool_call", "turn", True, 1.5)
    assert ok["args"] == {"direction": "left", "seconds": 1.5}
    assert (bad["ok"], bad["applied_seconds"]) == (False, None)
    assert "moonwalk" in bad["error"]
