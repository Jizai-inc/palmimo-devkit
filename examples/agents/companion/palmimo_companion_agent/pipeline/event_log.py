"""JSONL serialization for History events -- shared by stdout and file logging.

One event, one line, so a log can be tailed live or aggregated after the fact
(e.g. ``jq -r 'select(.kind=="tool_exec") | .name' events.jsonl | sort | uniq -c``).
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from typing import TYPE_CHECKING, TextIO

from .history import ToolExecEvent


if TYPE_CHECKING:
    from .history import Event


def emit_event(event: Event, *, out: TextIO) -> None:
    """Write one event as a single JSONL line to *out*, then flush.

    Tool events use ``args`` when serializable, otherwise ``arguments``.
    An ISO 8601 ``ts`` field timestamps every event. ASCII escaping keeps
    journald from treating C1 controls as binary; Portal's JSON.parse
    restores text for display.
    """
    timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    try:
        payload = dataclasses.asdict(event)
        if isinstance(event, ToolExecEvent):
            payload.pop("arguments" if event.args is not None else "args")
        payload["ts"] = timestamp
        line = json.dumps(payload, ensure_ascii=True, allow_nan=False)
    except (RecursionError, ValueError):
        if not isinstance(event, ToolExecEvent):
            raise
        payload = dataclasses.asdict(dataclasses.replace(event, args=None))
        payload.pop("args")
        payload["ts"] = timestamp
        line = json.dumps(payload, ensure_ascii=True, allow_nan=False)
    out.write(line + "\n")
    out.flush()
