"""Tests for :mod:`palmimo_companion_agent.pipeline.event_log`."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from typing import Any

import pytest

from palmimo_companion_agent.pipeline.event_log import emit_event
from palmimo_companion_agent.pipeline.history import CameraEvent, Event, KeyboardEvent, ToolExecEvent


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
    event = ToolExecEvent(
        tool_call_id="call-1", name="dance", arguments="{}", result="danced", thought="feeling it", args=None
    )
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
    assert json.loads(out.getvalue())["text"] == "こんにちは"


@pytest.mark.parametrize("text", ["\ud800", "\u0085", "\u009f", "\u2028", "\u2029", "\x00\n"])
@pytest.mark.parametrize("field", ["text", "summary", "tool_call_id", "name", "arguments", "result", "thought", "args"])
def test_emit_event_writes_unsafe_text_as_one_utf8_json_line(text: str, field: str) -> None:
    event: Event
    if field == "text":
        event = KeyboardEvent(text)
    elif field == "summary":
        event = CameraEvent(text)
    else:
        value: Any = {text: text} if field == "args" else text
        event = replace(ToolExecEvent("c", "nod", "{}", "done", args=None), **{field: value})
    buffer = io.BytesIO()
    out = io.TextIOWrapper(buffer, encoding="utf-8", errors="strict")
    emit_event(event, out=out)
    line = buffer.getvalue().decode("utf-8")
    assert len(line.splitlines()) == 1
    assert line.isascii()
    payload = json.loads(line)
    assert payload[field] == ({text: text} if field == "args" else text)


@pytest.mark.parametrize("failure", ["nan", "infinity", "deep", "huge_integer"])
def test_emit_event_falls_back_to_raw_arguments_when_args_cannot_be_serialized(failure: str) -> None:
    value: Any = {"nan": float("nan"), "infinity": float("inf"), "huge_integer": 10**5000}.get(failure, 0)
    if failure == "deep":
        for _ in range(10000):
            value = [value]
    out = io.StringIO()
    emit_event(ToolExecEvent("c", "nod", "raw arguments", "done", args={"value": value}), out=out)
    assert len(out.getvalue().splitlines()) == 1
    payload = json.loads(out.getvalue(), parse_constant=lambda value: pytest.fail(value))
    assert payload["arguments"] == "raw arguments"
    assert "args" not in payload
