"""Shared display inventory and backend for agent and MCP tests."""

import copy
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def load_display_assets() -> list[dict[str, Any]]:
    """Read the repository fixture shared with the docs browser runtime."""
    path = Path(__file__).parents[3] / "tests/fixtures/display-assets/catalog.json"
    return json.loads(path.read_text())["assets"]


class CatalogDisplay:
    def __init__(self, entries: list[dict[str, Any]] | None = None) -> None:
        self.is_connected = True
        self.entries = entries if entries is not None else load_display_assets()[:2]
        self.queries: list[tuple[tuple[str, ...], str | None]] = []
        self.calls: list[tuple[str, int | None]] = []
        self.failure: Exception | None = None

    def connect(self) -> None:
        self.is_connected = True

    def disconnect(self) -> None:
        self.is_connected = False

    def wake(self) -> str:
        return "OK WAKE"

    def idle(self) -> str:
        return "OK IDLE"

    def catalog(self, *, tags: Iterable[str] = (), locale: str | None = None) -> list[dict[str, Any]]:
        self.queries.append((tuple(tags), locale))
        if self.failure:
            raise self.failure
        return copy.deepcopy(self.entries)

    def play(self, id: str, hold_ms: int | None = None) -> str:
        self.calls.append((id, hold_ms))
        return f"OK PLAY {id}"
