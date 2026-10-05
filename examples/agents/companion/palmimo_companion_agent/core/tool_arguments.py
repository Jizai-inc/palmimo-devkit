"""Safe JSON object formatting for tool-call argument logs.

Numeric spellings may be normalized, such as ``1.0e0`` to ``1.0``.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any


#: Zero-width joiner: a format character that legitimately joins emoji, unlike
#: the bidi overrides and other format characters a terminal acts on.
_ZWJ = "\u200d"


def _is_terminal_control(char: str) -> bool:
    """Whether printing *char* raw would act on the operator's terminal (C1 controls, bidi overrides)."""
    category = unicodedata.category(char)
    return category == "Cc" or (category == "Cf" and char != _ZWJ)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def format_tool_arguments(arguments: str) -> str | None:
    """Return UTF-8-safe object JSON, or None to preserve the original string."""
    try:
        parsed = json.loads(arguments, object_pairs_hook=_unique_object)
        if not isinstance(parsed, dict):
            return None
        formatted = json.dumps(parsed, ensure_ascii=False, allow_nan=False)
        formatted.encode("utf-8", errors="strict")
        # json.dumps escapes only C0 controls; anything else decoded from a
        # \u escape would reach the journal and the terminal as a raw control.
        if any(_is_terminal_control(char) for char in formatted):
            return None
        return formatted
    except (ValueError, RecursionError, TypeError):
        return None
