"""The contract of the shared tool-argument log formatter."""

from __future__ import annotations

import json

import pytest

from palmimo_companion_agent.core.tool_arguments import format_tool_arguments


def test_format_tool_arguments_decodes_an_object_into_readable_json() -> None:
    formatted = format_tool_arguments('{"reason": "\\u58c1\\u306e\\u82b1\\u67c4", "seconds": 2}')

    assert formatted is not None
    assert "壁の花柄" in formatted
    assert json.loads(formatted) == {"reason": "壁の花柄", "seconds": 2}


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param('{"reason": "unterminated', id="malformed"),
        pytest.param('{"value": ' + "9" * 5000 + "}", id="oversized-integer"),
        pytest.param('{"value": NaN}', id="nan"),
        pytest.param('{"value": Infinity}', id="infinity"),
        pytest.param('{"value": -Infinity}', id="negative-infinity"),
        pytest.param('{"nested": [NaN]}', id="nested-nan"),
        pytest.param('{"v": 1e400}', id="overflow"),
        pytest.param('{"reason": "\\ud83d"}', id="unpaired-surrogate"),
        pytest.param('{"v":' + "[" * 100_000 + "0" + "]" * 100_000 + "}", id="deep-nesting"),
        pytest.param('{"a":1,"a":2}', id="duplicate-keys"),
        pytest.param('{"reason": "\\u009b2J"}', id="c1-control"),
        pytest.param('{"reason": "\\u202eabc"}', id="bidi-override"),
        pytest.param('"\\u58c1"', id="string"),
        pytest.param('[{"reason": "\\u58c1"}]', id="array"),
        pytest.param("42", id="number"),
        pytest.param("true", id="boolean"),
        pytest.param("null", id="null"),
    ],
)
def test_format_tool_arguments_keeps_the_raw_string_when_not_a_safe_object(arguments: str) -> None:
    assert format_tool_arguments(arguments) is None
