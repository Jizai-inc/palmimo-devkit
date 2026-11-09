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
from ..session import AI_INTENT_MAX_SECONDS, GESTURE_PLAY_SECONDS, PilotInput, PilotSession


_WALK_MOVES: dict[str, str] = {
    "forward": "forward",
    "backward": "backward",
    "left": "strafe_left",
    "right": "strafe_right",
}
_TURNS: dict[str, int] = {"left": -1, "right": 1}

_SECONDS_SCHEMA: dict[str, Any] = {
    "type": "number",
    "minimum": 0.5,
    "maximum": AI_INTENT_MAX_SECONDS,
    "description": f"How long to keep moving, in seconds (at most {AI_INTENT_MAX_SECONDS:g}).",
}

_FUNCTIONS: list[dict[str, Any]] = [
    {
        "name": "walk",
        "description": "Walk in a direction for a few seconds, then stop.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": sorted(_WALK_MOVES)},
                "seconds": _SECONDS_SCHEMA,
            },
            "required": ["direction", "seconds"],
        },
    },
    {
        "name": "turn",
        "description": "Turn in place to the left or right for a few seconds, then stop.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": sorted(_TURNS)},
                "seconds": _SECONDS_SCHEMA,
            },
            "required": ["direction", "seconds"],
        },
    },
    {
        "name": "gesture",
        "description": (
            "Play a gesture: wave hello, a short dance, nod yes, clap, do push-ups, tilt the body (curious), or bow."
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
            "Point the head for a few seconds, then it recenters. pitch and yaw run from -1 to 1; 0, 0 is straight ahead."
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
        "name": "stop",
        "description": "Stop moving and stand still.",
        "parameters": {"type": "object", "properties": {}},
    },
]

#: The motion tools in Chat Completions' `{"type": "function", "function": {...}}` shape.
TOOL_DEFINITIONS: list[dict[str, Any]] = [{"type": "function", "function": function} for function in _FUNCTIONS]


def _seconds(args: dict[str, Any]) -> float:
    value = args.get("seconds", 2.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("seconds must be a number")
    return float(value)


def run_tool(session: PilotSession, name: str, arguments: str) -> str:
    """Apply one tool call to *session* and return the JSON result the model sees.

    Never raises: a bad call (unknown tool, malformed arguments, or the AI
    mode having ended meanwhile) is reported back to the model as an error
    string, so it can correct itself rather than ending the session.

    Args:
        session (PilotSession): Receives the motion intent.
        name (str): The tool name the model called.
        arguments (str): The call's JSON-encoded arguments.

    Returns:
        str: `{"ok": true}` or `{"ok": false, "error": ...}`, JSON-encoded.
    """
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        intent, seconds = _intent_for(name, args)
    except (ValueError, KeyError, TypeError) as exc:
        return json.dumps({"ok": False, "error": str(exc)})
    if not session.set_ai_intent(intent, seconds):
        return json.dumps({"ok": False, "error": "AI mode is not running"})
    return json.dumps({"ok": True})


def _intent_for(name: str, args: dict[str, Any]) -> tuple[PilotInput, float]:
    if name == "walk":
        return PilotInput(move=_WALK_MOVES[args["direction"]]), _seconds(args)
    if name == "turn":
        return PilotInput(rotate=_TURNS[args["direction"]]), _seconds(args)
    if name == "gesture":
        gesture = args["name"]
        return PilotInput(gesture=gesture), GESTURE_PLAY_SECONDS[gesture]
    if name == "look":
        pitch, yaw = args["pitch"], args["yaw"]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (pitch, yaw)):
            raise ValueError("pitch and yaw must be numbers")
        return PilotInput(neck_pitch=clamp_neck(float(pitch)), neck_yaw=clamp_neck(float(yaw))), AI_INTENT_MAX_SECONDS
    if name == "stop":
        return PilotInput(), 0.0
    raise ValueError(f"unknown tool: {name}")


__all__ = ["TOOL_DEFINITIONS", "run_tool"]
