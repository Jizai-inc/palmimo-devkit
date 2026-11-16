# SPDX-License-Identifier: Apache-2.0
"""Shared display-name resolution; see API reference: Display names."""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable, Iterable
from typing import Any


EXPRESSION_ASSETS = {
    "happy": "happy",
    "smile": "happy",
    "laugh": "happy",
    "shy": "shy",
    "excited": "excited",
    "surprise": "surprise",
    "surprised": "surprise",
    "curious": "curious",
    "thinking": "thinking",
    "angry": "angry",
    "sad": "sad",
    "sleepy": "sleepy",
    "love": "love",
    "sleep": "sleeping",
    "sleeping": "sleeping",
    "maru": "maru",
    "batsu": "batsu",
    "groove": "groove",
}


def resolve_asset_name(
    name: str,
    app_id: str | None,
    asset_ids: Callable[[], Iterable[str]],
    *,
    allow_aliases: bool = False,
) -> str:
    """Resolve an input using the API reference's Display names contract."""
    normalized = name.strip().lower()
    if "/" in normalized:
        return normalized
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,39}", normalized):
        raise ValueError("Expected a display name or catalog asset id")
    available = asset_ids()
    ids = available if isinstance(available, (set, frozenset)) else set(available)
    owner = app_id.strip().lower() if app_id else None
    candidate = f"{owner}/{normalized}"
    if owner and candidate in ids:
        return candidate
    candidate = "palmimo/" + normalized
    if candidate in ids:
        return candidate
    alias = EXPRESSION_ASSETS.get(normalized) if allow_aliases else None
    if alias is not None and "palmimo/" + alias in ids:
        warnings.warn("Legacy display aliases are deprecated; use catalog names", DeprecationWarning, stacklevel=2)
        return "palmimo/" + alias
    raise LookupError("Use play() with a catalog asset id, or declare the name as an application asset")


def named_catalog(catalog: Iterable[dict[str, Any]], app_id: str | None) -> list[dict[str, Any]]:
    """Attach the shortest unambiguous input for each catalog entry."""
    entries = list(catalog)
    ids = {asset["id"] for asset in entries}
    owner = app_id.strip().lower() if app_id else None
    result = []
    for asset in entries:
        id = asset["id"]
        if "name" in asset:
            result.append(dict(asset))
            continue
        short = id.split("/", 1)[1]
        try:
            resolved = resolve_asset_name(short, owner, lambda: ids)
            name = short if resolved == id else id
        except (LookupError, ValueError):
            name = id
        result.append({**asset, "name": name})
    return result
