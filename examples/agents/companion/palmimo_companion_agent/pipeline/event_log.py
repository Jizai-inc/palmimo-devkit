"""JSONL serialization for History events -- shared by stdout and file logging.

One event, one line, so a log can be tailed live or aggregated after the fact
(e.g. ``jq -r 'select(.kind=="tool_exec") | .name' events.jsonl | sort | uniq -c``).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
from datetime import datetime
from typing import TYPE_CHECKING, TextIO


if TYPE_CHECKING:
    from .history import Event


def _reject_constant(name: str) -> None:
    """Refuse ``NaN`` / ``Infinity``: Python parses them, but a line carrying one is not JSON."""
    raise ValueError(name)


def emit_event(event: Event, *, out: TextIO) -> None:
    """Write one event as a single JSONL line to *out*, then flush.

    The payload is ``dataclasses.asdict(event)`` plus a millisecond-precision,
    timezone-aware ISO 8601 ``ts`` field, so log lines sort and filter cleanly
    with tools like ``jq``. Flushing every line keeps the log current for a
    live ``tail -f`` even if the process later exits uncleanly.

    A tool call's ``arguments`` -- the JSON string the LLM returned -- is
    written as the object it parses to, so the line is not JSON inside a JSON
    string. Arguments that are not a JSON object are written as the string
    they are, so the field is either an object or the raw text.
    """
    payload = dataclasses.asdict(event)
    arguments = payload.get("arguments")
    if isinstance(arguments, str):
        with contextlib.suppress(ValueError):
            parsed = json.loads(arguments, parse_constant=_reject_constant)
            if isinstance(parsed, dict):
                payload["arguments"] = parsed
    payload["ts"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
    out.write(json.dumps(payload, ensure_ascii=False) + "\n")
    out.flush()
