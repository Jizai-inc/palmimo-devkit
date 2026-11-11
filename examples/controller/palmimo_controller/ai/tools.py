"""The motion tools an AI mode may call, and how each one becomes a session intent.

The AI never touches `Palmimo` directly: `robot_loop.RobotLoop` is the only
thread allowed to, so a tool call only records an intent in
`session.PilotSession` and the robot loop applies it on its next tick. That is
also what keeps the robot connection unchanged across a mode switch.
"""

from __future__ import annotations

import json
from typing import Any

from ..control import clamp_neck
from ..session import (
    AI_FACE_MAX_SECONDS,
    AI_TOOL_MAX_SECONDS,
    FACE_EXPRESSIONS,
    GESTURE_PLAY_SECONDS,
    PilotInput,
    PilotSession,
)


_WALK_MOVES: dict[str, str] = {
    "forward": "forward",
    "backward": "backward",
    "left": "strafe_left",
    "right": "strafe_right",
}
_TURNS: dict[str, int] = {"left": -1, "right": 1}


def _seconds_schema(tool: str) -> dict[str, Any]:
    cap = AI_TOOL_MAX_SECONDS[tool]
    return {
        "type": "number",
        "minimum": 0.5,
        "maximum": cap,
        "description": f"How long to keep moving, in seconds (at most {cap:g}).",
    }


_FUNCTIONS: list[dict[str, Any]] = [
    {
        "name": "walk",
        "description": f"Walk in a direction for up to {AI_TOOL_MAX_SECONDS['walk']:g} seconds, then stop.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": sorted(_WALK_MOVES)},
                "seconds": _seconds_schema("walk"),
            },
            "required": ["direction", "seconds"],
        },
    },
    {
        "name": "turn",
        "description": (
            f"Turn in place to the left or right for up to {AI_TOOL_MAX_SECONDS['turn']:g} seconds, then stop."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": sorted(_TURNS)},
                "seconds": _seconds_schema("turn"),
            },
            "required": ["direction", "seconds"],
        },
    },
    {
        "name": "gesture",
        "description": (
            "Play a gesture: wave hello, wave with both arms (big hello), a short dance, nod yes, "
            "shake the head no, clap, do push-ups, tilt the body (curious), stretch, or bow."
        ),
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "enum": sorted(GESTURE_PLAY_SECONDS)}},
            "required": ["name"],
        },
    },
    {
        "name": "look",
        "description": (
            f"Point the head for {AI_TOOL_MAX_SECONDS['look']:g} seconds, then it recenters. "
            "pitch and yaw run from -1 to 1; 0, 0 is straight ahead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pitch": {"type": "number", "minimum": -1, "maximum": 1},
                "yaw": {"type": "number", "minimum": -1, "maximum": 1},
            },
            "required": ["pitch", "yaw"],
        },
    },
    {
        "name": "set_face",
        "description": (
            "Show a facial expression on the face display for a few seconds, then the face returns to neutral."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "enum": list(FACE_EXPRESSIONS)},
                "seconds": {
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": AI_FACE_MAX_SECONDS,
                    "description": f"How long to show it, in seconds (at most {AI_FACE_MAX_SECONDS:g}).",
                },
            },
            "required": ["expression", "seconds"],
        },
    },
    {
        "name": "stop",
        "description": "Stop moving and stand still.",
        "parameters": {"type": "object", "properties": {}},
    },
]

#: The motion tools in the Realtime API's flat `{"type": "function", "name": ...}` shape.
TOOL_DEFINITIONS: list[dict[str, Any]] = [{"type": "function", **function} for function in _FUNCTIONS]


def _seconds(tool: str, args: dict[str, Any]) -> float:
    """The call's `seconds`, clamped to *tool*'s cap: the schema's maximum is advice the model may ignore."""
    value = args.get("seconds", 2.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("seconds must be a number")
    return max(0.0, min(float(value), AI_TOOL_MAX_SECONDS[tool]))


def run_tool(session: PilotSession, name: str, arguments: str) -> str:
    """Apply one tool call to *session* and return the JSON result the model sees.

    Never raises: a bad call (unknown tool, malformed arguments, or the AI
    mode having ended meanwhile) is reported back to the model as an error
    string, so it can correct itself rather than ending the session.

    Args:
        session (PilotSession): Receives the motion intent or face.
        name (str): The tool name the model called.
        arguments (str): The call's JSON-encoded arguments.

    Returns:
        str: `{"ok": true}` or `{"ok": false, "error": ...}`, JSON-encoded.
    """
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        if name == "set_face":
            # A face is not a motion: it must not end a gesture the robot is playing.
            expression, seconds = _face_for(args)
            applied = session.set_ai_face(expression, seconds)
        else:
            intent, seconds = _intent_for(name, args)
            applied = session.set_ai_intent(intent, seconds)
    except (ValueError, KeyError, TypeError) as exc:
        return json.dumps({"ok": False, "error": str(exc)})
    if not applied:
        return json.dumps({"ok": False, "error": "AI mode is not running"})
    return json.dumps({"ok": True})


def _face_for(args: dict[str, Any]) -> tuple[str, float]:
    expression = args["expression"]
    if not isinstance(expression, str) or expression.upper() not in FACE_EXPRESSIONS:
        raise ValueError(f"expression must be one of {', '.join(FACE_EXPRESSIONS)}")
    value = args.get("seconds", 2.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("seconds must be a number")
    return expression.upper(), max(0.0, min(float(value), AI_FACE_MAX_SECONDS))


def _intent_for(name: str, args: dict[str, Any]) -> tuple[PilotInput, float]:
    if name == "walk":
        return PilotInput(move=_WALK_MOVES[args["direction"]]), _seconds("walk", args)
    if name == "turn":
        return PilotInput(rotate=_TURNS[args["direction"]]), _seconds("turn", args)
    if name == "gesture":
        gesture = args["name"]
        if gesture not in GESTURE_PLAY_SECONDS:
            raise ValueError(f"unknown gesture: {gesture}")
        return PilotInput(gesture=gesture), GESTURE_PLAY_SECONDS[gesture]
    if name == "look":
        pitch, yaw = args["pitch"], args["yaw"]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (pitch, yaw)):
            raise ValueError("pitch and yaw must be numbers")
        return PilotInput(neck_pitch=clamp_neck(float(pitch)), neck_yaw=clamp_neck(float(yaw))), AI_TOOL_MAX_SECONDS[
            "look"
        ]
    if name == "stop":
        return PilotInput(), 0.0
    raise ValueError(f"unknown tool: {name}")


__all__ = ["TOOL_DEFINITIONS", "run_tool"]
