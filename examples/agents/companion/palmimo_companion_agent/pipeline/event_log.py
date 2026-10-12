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


def emit_event(event: Event, *, out: TextIO) -> None:
    """Write one event as a single JSONL line to *out*, then flush.

    The payload is ``dataclasses.asdict(event)`` plus a millisecond-precision,
    timezone-aware ISO 8601 ``ts`` field, so log lines sort and filter cleanly
    with tools like ``jq``. Flushing every line keeps the log current for a
    live ``tail -f`` even if the process later exits uncleanly.

    A tool call's ``arguments`` -- the JSON string the LLM returned -- is
    written as the value it parses to, so the line is not JSON inside a JSON
    string. Arguments that do not parse are written as the string they are.
    """
    payload = dataclasses.asdict(event)
    arguments = payload.get("arguments")
    if isinstance(arguments, str):
        with contextlib.suppress(ValueError):
            payload["arguments"] = json.loads(arguments)
    payload["ts"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
    out.write(json.dumps(payload, ensure_ascii=False) + "\n")
    out.flush()
