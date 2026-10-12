"""Argument validation shared by tool dispatch front ends."""

from __future__ import annotations

import json
from typing import Any


def parse_tool_arguments(arguments: str) -> dict[str, Any] | str:
    """Return a parsed argument object or an execution error for an invalid call."""
    try:
        args = json.loads(arguments or "{}")
    except (ValueError, RecursionError):
        args = None
    if isinstance(args, dict):
        return args
    return f"execution error: tool arguments were not a JSON object: {arguments[:200]}"


def serialize_tool_arguments(
    value: dict[str, Any] | str,
    fallback: dict[str, Any] | str,
    *,
    ensure_ascii: bool = True,
) -> str:
    """Serialize an argument-bearing value as strict JSON, or its raw-argument fallback."""
    try:
        return json.dumps(value, ensure_ascii=ensure_ascii, allow_nan=False)
    except (ValueError, RecursionError):
        return json.dumps(fallback, ensure_ascii=ensure_ascii, allow_nan=False)
