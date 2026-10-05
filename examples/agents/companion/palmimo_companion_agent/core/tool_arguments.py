"""Shared JSON object parsing for tool-call argument logs."""

from __future__ import annotations

import json
from typing import Any, NoReturn


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"Invalid JSON constant: {value}")


def parse_tool_arguments(arguments: str) -> dict[str, Any] | str:
    """Return a parsed JSON object, or preserve the original argument string."""
    try:
        parsed = json.loads(arguments, parse_constant=_reject_constant)
    except ValueError:
        return arguments
    return parsed if isinstance(parsed, dict) else arguments
