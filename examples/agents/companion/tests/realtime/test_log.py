"""Tests for :mod:`palmimo_companion_agent.realtime.log` -- the minimal JSONL event log."""

from __future__ import annotations

import json
from pathlib import Path

from palmimo_companion_agent.realtime.log import EventLog


def test_a_none_path_makes_every_write_a_no_op() -> None:
    log = EventLog.open(None)
    log.write("tool_call", name="nod")  # must not raise
    log.close()


def test_open_writes_one_jsonl_line_per_call(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog.open(path)

    log.write("tool_call", name="nod", call_id="c1")
    log.write("unknown_event", event_type="rate_limits.updated")
    log.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["kind"] == "tool_call"
    assert first["name"] == "nod"
    assert first["call_id"] == "c1"
    assert "ts" in first
    second = json.loads(lines[1])
    assert second == {"kind": "unknown_event", "event_type": "rate_limits.updated", "ts": second["ts"]}


def test_open_creates_the_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "events.jsonl"
    log = EventLog.open(path)
    log.write("tool_call", name="nod")
    log.close()
    assert path.exists()
