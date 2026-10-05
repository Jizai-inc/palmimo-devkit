"""Safe JSON object formatting for tool-call argument logs.

Numeric spellings may be normalized, such as ``1.0e0`` to ``1.0``.
"""

from __future__ import annotations

import json
from typing import Any


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
        return formatted
    except (ValueError, RecursionError, TypeError):
        return None
