"""Tests for :mod:`palmimo_companion_agent.pipeline.event_log`."""

from __future__ import annotations

import io
import json

from palmimo_companion_agent.pipeline.event_log import emit_event
from palmimo_companion_agent.pipeline.history import KeyboardEvent, ToolExecEvent


def test_emit_event_writes_exactly_one_line() -> None:
    out = io.StringIO()
    emit_event(KeyboardEvent("hello"), out=out)
    lines = out.getvalue().splitlines()
    assert len(lines) == 1


def test_emit_event_line_is_valid_json_with_event_fields_and_timestamp() -> None:
    out = io.StringIO()
    emit_event(KeyboardEvent("hello"), out=out)
    payload = json.loads(out.getvalue())
    assert payload["kind"] == "keyboard"
    assert payload["text"] == "hello"
    assert "ts" in payload


def test_emit_event_preserves_all_dataclass_fields() -> None:
    out = io.StringIO()
    event = ToolExecEvent(tool_call_id="call-1", name="dance", arguments="{}", result="danced", thought="feeling it")
    emit_event(event, out=out)
    payload = json.loads(out.getvalue())
    assert payload["tool_call_id"] == "call-1"
    assert payload["name"] == "dance"
    assert payload["result"] == "danced"
    assert payload["thought"] == "feeling it"


def test_emit_event_appends_without_clobbering_prior_lines() -> None:
    out = io.StringIO()
    emit_event(KeyboardEvent("one"), out=out)
    emit_event(KeyboardEvent("two"), out=out)
    lines = out.getvalue().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["text"] == "one"
    assert json.loads(lines[1])["text"] == "two"


def test_emit_event_keeps_non_ascii_text_readable() -> None:
    out = io.StringIO()
    emit_event(KeyboardEvent("こんにちは"), out=out)
    assert "こんにちは" in out.getvalue()
