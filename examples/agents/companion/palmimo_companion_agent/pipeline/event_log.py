"""JSONL serialization for History events -- shared by stdout and file logging.

One event, one line, so a log can be tailed live or aggregated after the fact
(e.g. ``jq -r 'select(.kind=="tool_exec") | .name' events.jsonl | sort | uniq -c``).
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from typing import TYPE_CHECKING, TextIO

from ..core.tool_arguments import parse_tool_arguments
from .history import ToolExecEvent


if TYPE_CHECKING:
    from .history import Event


def emit_event(event: Event, *, out: TextIO) -> None:
    """Write one event as a single JSONL line to *out*, then flush.

    The payload is ``dataclasses.asdict(event)`` plus a millisecond-precision,
    timezone-aware ISO 8601 ``ts`` field, so log lines sort and filter cleanly
    with tools like ``jq``. See :class:`~.history.ToolExecEvent` for the
    argument field's meaning; its JSON objects are logged as readable objects,
    and other inputs are preserved as strings.
    Flushing every line keeps the log current for a live ``tail -f`` even if
    the process later exits uncleanly.
    """
    payload = dataclasses.asdict(event)
    if isinstance(event, ToolExecEvent):
        payload["arguments"] = parse_tool_arguments(event.arguments)
    payload["ts"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
    out.write(json.dumps(payload, ensure_ascii=False) + "\n")
    out.flush()
