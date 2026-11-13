"""System prompt loading and per-turn message assembly for the chat loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from palmimo_sdk.agent import render_display_prompt

from .history import ToolExecEvent


if TYPE_CHECKING:
    from .history import History


#: core/prompts/*.md ship in the shared character core, not this pipeline
#: runtime (see this repository's root AGENTS.md's Comments section, and
#: RUNTIME_TEXT_FILES in tests/contracts/test_comment_language.py, for why
#: they stay Japanese) -- a future realtime/ runtime reads the same files.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "core" / "prompts"
#: The character-swap customization point: name, personality, likes/dislikes,
#: how self-referential questions get answered. Everything else
#: (identity.md's physical/hardware facts and tool contract, and each turn
#: kind's own behavior rules) is the same regardless of which character this
#: names -- persona.md is the one file a user edits to reskin the robot.
_PERSONA_PATH = _PROMPTS_DIR / "persona.md"
_IDENTITY_PATH = _PROMPTS_DIR / "identity.md"

#: How many of the most recent tool calls recent_moves_hint() looks at.
_RECENT_TOOL_WINDOW = 8
#: Of those, how many are considered for the face breakdown -- a face chosen
#: 8 tools ago is no longer "what I just did".
_RECENT_FACE_WINDOW = 5
_MAX_FACE_SUGGESTIONS = 4


def load_prompt(kind: Literal["idle", "respond"], *, catalog: list[dict[str, Any]] | None = None) -> str:
    """Load the prompt template; optionally render a connected catalog snapshot."""
    persona = _PERSONA_PATH.read_text(encoding="utf-8")
    identity = _IDENTITY_PATH.read_text(encoding="utf-8")
    turn = (_PROMPTS_DIR / f"{kind}.md").read_text(encoding="utf-8")
    template = f"{persona}\n\n{identity}\n\n{turn}"
    return render_display_prompt(template, catalog) if catalog is not None else template


def recent_moves_hint(history: History, catalog: list[dict[str, Any]] | None = None) -> str | None:
    """Build a "don't repeat yourself" hint from the last few tool calls.

    A small model tends to fall back on the same one or two tools; this
    surfaces what it just did so the system prompt's rotation rule has
    something concrete to act on instead of relying on the model to recall
    its own history from the raw message list. Returns None if no tool has
    been called yet (nothing to reference).
    """
    recent: list[ToolExecEvent] = []
    for event in reversed(history.events):
        if isinstance(event, ToolExecEvent):
            recent.append(event)
            if len(recent) >= _RECENT_TOOL_WINDOW:
                break
    if not recent:
        return None

    tools: list[str] = []
    for event in recent:
        if event.name not in tools:
            tools.append(event.name)

    allowed = {entry["id"] for entry in catalog or []}
    faces: list[str] = []
    for event in recent[:_RECENT_FACE_WINDOW]:
        args = _safe_args(event.arguments)
        face = args.get("id") if event.name == "play_asset" else args.get("face")
        if isinstance(face, str) and face in allowed and face not in faces:
            faces.append(face)

    tools_str = ", ".join(tools) if tools else "(none)"
    faces_str = ", ".join(faces) if faces else "(none)"

    face_candidates = [entry["id"] for entry in catalog or [] if entry["id"] not in faces][:_MAX_FACE_SUGGESTIONS]
    face_hint = (
        f"Faces to try: {' / '.join(face_candidates)}."
        if face_candidates
        else "Vary the face away from what you just used."
    )

    lines = [
        f"[recent moves] tools (newest first): {tools_str} / faces: {faces_str}",
        f"Prefer a tool and asset not on that list. {face_hint}",
    ]
    if allowed and not any(event.name == "play_asset" for event in recent):
        lines.append("No play_asset in the recent window -- consider a display action.")
    return "\n".join(lines)


def _safe_args(arguments: str) -> dict:
    """Best-effort JSON-decode *arguments* into a dict, or {} if that fails."""
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_messages(
    history: History,
    system_prompt: str,
    *,
    continuation_nudge: str,
    display_catalog: list[dict[str, Any]] | None = None,
) -> list[dict]:
    """Assemble system + recent-moves hint + history into chat() messages.

    Appends *continuation_nudge* (never stored in history -- it's ephemeral,
    added fresh to the message list built for each chat() call) when the
    window ends on a tool turn: gemini flash-lite tends to return an empty
    response when the message list ends on a tool (function response) turn,
    and appending a user-role nudge only in that case breaks the stall
    without ever changing what the model is asked to do. The wording is
    caller-specific -- :class:`~.idle.IdleTurn` asks for a single tool call,
    :class:`~.respond.RespondTurn` for a plan of up to 4, since a shared
    single-tool wording would contradict respond's own multi-call contract
    -- so it is passed in rather than owned here.
    """
    system_prompt = render_display_prompt(system_prompt, display_catalog or [])
    hint = recent_moves_hint(history, display_catalog)
    system = f"{system_prompt}\n\n{hint}" if hint else system_prompt
    messages = [{"role": "system", "content": system}, *history.to_messages()]
    if messages[-1].get("role") == "tool":
        messages.append({"role": "user", "content": continuation_nudge})
    return messages
