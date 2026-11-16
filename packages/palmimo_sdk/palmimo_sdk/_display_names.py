# SPDX-License-Identifier: Apache-2.0
"""Shared deprecated-name resolution; see API reference: Migration from 0.1.x."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable


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


def resolve_asset_name(name: str, app_id: str | None, asset_ids: Callable[[], Iterable[str]]) -> str:
    """Resolve a display name using the API reference's 0.1.x migration rules."""
    if "/" in name:
        return name
    normalized = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,39}", normalized):
        raise ValueError("Expected a display name or catalog asset id")
    standard = EXPRESSION_ASSETS.get(normalized)
    if standard is not None:
        return "palmimo/" + standard
    candidate = f"{app_id}/{normalized}"
    if app_id and candidate in asset_ids():
        return candidate
    raise LookupError("Use play() with a catalog asset id, or declare the name as an application asset")
