"""Tests for :mod:`palmimo_companion_agent.pipeline.prompt`."""

from __future__ import annotations

import json

from palmimo_companion_agent.pipeline.history import (
    History,
    KeyboardEvent,
    SystemNoteEvent,
    ToolExecEvent,
)
from palmimo_companion_agent.pipeline.prompt import build_messages, load_prompt, recent_moves_hint


def _tool_event(name: str, arguments: dict | None = None, *, tool_call_id: str = "c") -> ToolExecEvent:
    return ToolExecEvent(
        tool_call_id=tool_call_id,
        name=name,
        arguments=json.dumps(arguments or {}),
        result="ok",
    )


def test_load_prompt_returns_non_empty_text_for_idle_and_respond() -> None:
    idle = load_prompt("idle")
    respond = load_prompt("respond")
    assert isinstance(idle, str)
    assert isinstance(respond, str)
    assert len(idle) > 0
    assert len(respond) > 0


def test_load_prompt_composes_persona_then_identity_then_the_kind_specific_prompt() -> None:
    idle = load_prompt("idle")
    respond = load_prompt("respond")
    # Both share the same persona.md, so a persona-only phrase must appear in both...
    assert "パルミーモ" in idle
    assert "パルミーモ" in respond
    # ...and both share the same identity.md too (hardware facts / tool contract).
    assert "物理・ハード制約と tool との契約" in idle
    assert "物理・ハード制約と tool との契約" in respond
    # But each also carries kind-specific content the other doesn't.
    assert "自律ループ" in idle
    assert "自律ループ（誰も話しかけていない時間）" not in respond
    assert "複数 tool call を並べてよい" in respond
    assert "複数 tool call を並べてよい" not in idle


def test_load_prompt_orders_persona_before_identity_before_the_kind_specific_prompt() -> None:
    idle = load_prompt("idle")
    persona_index = idle.index("パルミーモ")
    identity_index = idle.index("物理・ハード制約と tool との契約")
    kind_index = idle.index("自律ループ")
    assert persona_index < identity_index < kind_index


def test_load_prompt_idle_forbids_say() -> None:
    idle = load_prompt("idle")
    assert "say" in idle  # the idle prompt explains that say is unavailable...
    assert "発話は一切しない" in idle  # ...as an explicit rule.


def test_recent_moves_hint_returns_none_when_no_tool_has_run() -> None:
    assert recent_moves_hint(History()) is None


def test_recent_moves_hint_lists_recent_tool_names_newest_first() -> None:
    history = History()
    history.add(_tool_event("forward"))
    history.add(_tool_event("dance"))
    hint = recent_moves_hint(history)
    assert hint is not None
    assert "dance, forward" in hint


def test_recent_moves_hint_deduplicates_repeated_tool_names() -> None:
    history = History()
    history.add(_tool_event("dance"))
    history.add(_tool_event("forward"))
    history.add(_tool_event("dance"))
    hint = recent_moves_hint(history)
    assert hint is not None
    assert hint.count("dance") == 1


def test_recent_moves_hint_only_considers_the_last_eight_tool_calls() -> None:
    history = History()
    history.add(_tool_event("ancient_tool"))
    for _ in range(8):
        history.add(_tool_event("filler"))
    hint = recent_moves_hint(history)
    assert hint is not None
    assert "ancient_tool" not in hint


def test_recent_moves_hint_reports_recent_faces() -> None:
    history = History()
    history.add(_tool_event("set_face", {"name": "HAPPY"}))
    hint = recent_moves_hint(history)
    assert hint is not None
    assert "HAPPY" in hint
    # HAPPY was just used, so it must not be re-suggested as a fresh pick.
    assert "Faces to try" not in hint or "HAPPY" not in hint.split("Faces to try:")[1].split(".")[0]


def test_recent_moves_hint_reports_recent_emoji() -> None:
    history = History()
    history.add(_tool_event("show_emoji", {"name": "SUN", "seconds": 3.0}))
    hint = recent_moves_hint(history)
    assert hint is not None
    assert "SUN" in hint


def test_recent_moves_hint_suggests_display_when_none_used_recently() -> None:
    history = History()
    history.add(_tool_event("forward"))
    hint = recent_moves_hint(history)
    assert hint is not None
    assert "set_face" in hint or "show_emoji" in hint


_NUDGE = "[continued] test nudge"


def test_build_messages_puts_system_prompt_first() -> None:
    messages = build_messages(History(), "SYSTEM PROMPT TEXT", continuation_nudge=_NUDGE)
    assert messages[0]["role"] == "system"
    assert "SYSTEM PROMPT TEXT" in messages[0]["content"]


def test_build_messages_appends_hint_to_system_content_when_present() -> None:
    history = History()
    history.add(_tool_event("dance"))
    messages = build_messages(history, "BASE", continuation_nudge=_NUDGE)
    assert "BASE" in messages[0]["content"]
    assert "dance" in messages[0]["content"]


def test_build_messages_includes_history_messages() -> None:
    history = History()
    history.add(KeyboardEvent("say hi"))
    messages = build_messages(history, "BASE", continuation_nudge=_NUDGE)
    assert {"role": "user", "content": "[instruction] say hi"} in messages


def test_build_messages_appends_the_given_continuation_nudge_when_window_ends_on_tool_turn() -> None:
    history = History()
    history.add(_tool_event("dance"))
    messages = build_messages(history, "BASE", continuation_nudge=_NUDGE)
    assert messages[-1] == {"role": "user", "content": _NUDGE}


def test_build_messages_uses_the_caller_specific_nudge_wording() -> None:
    """idle and respond pass different wording (single tool vs. up-to-4 plan) -- confirm
    build_messages doesn't own or hardcode either, just relays whatever it's given."""
    history = History()
    history.add(_tool_event("dance"))
    idle_messages = build_messages(history, "BASE", continuation_nudge="[continued] idle wording")
    respond_messages = build_messages(history, "BASE", continuation_nudge="[continued] respond wording")
    assert idle_messages[-1]["content"] == "[continued] idle wording"
    assert respond_messages[-1]["content"] == "[continued] respond wording"


def test_build_messages_does_not_append_nudge_when_window_ends_on_user_turn() -> None:
    history = History()
    history.add(SystemNoteEvent("hello"))
    messages = build_messages(history, "BASE", continuation_nudge=_NUDGE)
    assert messages[-1] == {"role": "user", "content": "hello"}
