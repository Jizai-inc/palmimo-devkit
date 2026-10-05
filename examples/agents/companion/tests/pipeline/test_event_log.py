"""Tests for :mod:`palmimo_companion_agent.pipeline.event_log`."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass

import pytest

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


def test_emit_event_logs_tool_arguments_as_readable_object() -> None:
    out = io.StringIO()
    arguments = '{"reason": "\\u58c1\\u306e\\u82b1\\u67c4"}'
    emit_event(ToolExecEvent(tool_call_id="call-1", name="discover", arguments=arguments, result="ok"), out=out)
    assert json.loads(out.getvalue())["arguments"] == {"reason": "壁の花柄"}
    assert "壁の花柄" in out.getvalue()
    assert "\\u58c1" not in out.getvalue()


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param('{"reason": "unterminated', id="malformed"),
        pytest.param('{"value": ' + "9" * 5000 + "}", id="oversized-integer"),
        pytest.param('{"value": NaN}', id="nan"),
        pytest.param('{"value": Infinity}', id="infinity"),
        pytest.param('{"value": -Infinity}', id="negative-infinity"),
        pytest.param('{"nested": [NaN]}', id="nested-nan"),
    ],
)
def test_emit_event_preserves_tool_arguments_when_json_is_invalid(arguments: str) -> None:
    out = io.StringIO()
    emit_event(ToolExecEvent(tool_call_id="call-1", name="discover", arguments=arguments, result="ok"), out=out)
    payload = json.loads(out.getvalue(), parse_constant=lambda value: pytest.fail(f"Invalid JSON constant: {value}"))
    assert payload["arguments"] == arguments
    assert payload["result"] == "ok"
    assert len(out.getvalue().splitlines()) == 1


@pytest.mark.parametrize("arguments", ['"\\u58c1"', '[{"reason": "\\u58c1"}]', "42", "true", "null"])
def test_emit_event_preserves_tool_arguments_when_json_is_not_object(arguments: str) -> None:
    out = io.StringIO()
    emit_event(ToolExecEvent(tool_call_id="call-1", name="discover", arguments=arguments, result="ok"), out=out)
    assert json.loads(out.getvalue())["arguments"] == arguments


def test_emit_event_preserves_arguments_on_non_tool_event() -> None:
    @dataclass(frozen=True)
    class ArgumentsEvent(KeyboardEvent):
        arguments: str = "{}"

    out = io.StringIO()
    emit_event(ArgumentsEvent("hello"), out=out)
    assert json.loads(out.getvalue())["arguments"] == "{}"
