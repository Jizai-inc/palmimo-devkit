"""JSONL serialization for History events -- shared by stdout and file logging.

One event, one line, so a log can be tailed live or aggregated after the fact
(e.g. ``jq -r 'select(.kind=="tool_exec") | .name' events.jsonl | sort | uniq -c``).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, TextIO


if TYPE_CHECKING:
    from ..code_version import CodeVersion
    from .history import Event
    from .settings import PipelineSettings


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
        # RecursionError: deeply nested input; the UTF-8 encode catches lone surrogates.
        with contextlib.suppress(ValueError, RecursionError):
            parsed = json.loads(arguments, parse_constant=_reject_constant)
            if isinstance(parsed, dict):
                json.dumps(parsed, ensure_ascii=False).encode("utf-8")
                payload["arguments"] = parsed
    _write_line(payload, out=out)


def emit_run_start(version: CodeVersion, settings: PipelineSettings, *, out: TextIO) -> None:
    """Write the ``run_start`` line that opens one process's events in *out*.

    It names the code the process started from and a hash of its resolved
    settings, so a log read later says which code and configuration wrote the
    lines after it. The settings carry no API keys (see
    :meth:`~palmimo_companion_agent.settings.CompanionSettings.with_env_file`).
    """
    resolved = json.dumps(settings.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    _write_line(
        {
            "kind": "run_start",
            **dataclasses.asdict(version),
            "settings_sha256": hashlib.sha256(resolved.encode("utf-8")).hexdigest(),
        },
        out=out,
    )


def _write_line(payload: dict[str, Any], *, out: TextIO) -> None:
    payload["ts"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
    out.write(json.dumps(payload, ensure_ascii=False) + "\n")
    out.flush()
