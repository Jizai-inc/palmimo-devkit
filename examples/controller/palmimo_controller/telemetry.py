"""Structured events: sessions, mode switches, AI replies and tool calls, reflexes, and idle.

Each event is one JSON log record on the `palmimo_controller.telemetry`
logger, so the host can collect them from the app's output without this app
knowing where they go. The robot's own spoken text is recorded (`ai_said`);
the visitor's speech is never transcribed, and no audio is recorded.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any


_LOG = logging.getLogger("palmimo_controller.telemetry")


class Telemetry:
    """Emits events tagged with one id for this app run."""

    def __init__(
        self,
        *,
        emit: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
        session_id: str | None = None,
    ) -> None:
        self._emit = emit or _LOG.info
        self._clock = clock
        self.session_id = session_id or uuid.uuid4().hex

    def event(self, kind: str, **fields: Any) -> None:
        """Record one event of *kind* with *fields* (JSON-serializable values only)."""
        record = {"type": kind, "session_id": self.session_id, "ts": self._clock(), **fields}
        self._emit(json.dumps(record, ensure_ascii=False, default=str))


__all__ = ["Telemetry"]
