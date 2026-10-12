"""Argument validation shared by tool dispatch front ends."""

from __future__ import annotations

import json
from typing import Any


def parse_tool_arguments(arguments: str) -> tuple[dict[str, Any] | None, str | None]:
    """Return a parsed argument object or an execution error for an invalid call."""
    try:
        args = json.loads(arguments or "{}")
    except (ValueError, RecursionError):
        args = None
    if isinstance(args, dict):
        return args, None
    return None, f"execution error: tool arguments were not a JSON object: {arguments[:200]}"
