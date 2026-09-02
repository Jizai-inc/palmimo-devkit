"""A minimal JSONL event log for the realtime runtime.

Deliberately not :mod:`palmimo_companion_agent.pipeline.event_log`: that
module serializes a :class:`~palmimo_companion_agent.pipeline.history.Event`
dataclass, which this runtime does not have (see ``state.py``'s docstring for
the same "no History here" boundary). This is a plain ``kind`` + free-form
fields writer instead -- one line per call, no schema to keep in sync with a
dataclass hierarchy that does not exist over here.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Self


class EventLog:
    """Append-only JSONL writer. A ``None`` path makes every write a no-op."""

    def __init__(self, out: Any | None) -> None:
        self._out = out

    @classmethod
    def open(cls, path: Path | str | None) -> Self:
        """Open *path* for appending, or build a no-op log if *path* is ``None``."""
        if path is None:
            return cls(None)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(path.open("a", encoding="utf-8"))

    def write(self, kind: str, **fields: Any) -> None:
        """Append one JSONL line: ``{"kind": ..., "ts": ..., **fields}``. Flushed immediately."""
        if self._out is None:
            return
        payload = {"kind": kind, "ts": time.time(), **fields}
        self._out.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._out.flush()

    def close(self) -> None:
        """Close the underlying file, if one is open."""
        if self._out is not None:
            self._out.close()


__all__ = ["EventLog"]
