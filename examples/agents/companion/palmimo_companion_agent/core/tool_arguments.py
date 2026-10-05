"""Safe JSON object formatting for tool-call argument logs.

Numbers go through a float round trip, so the log may show ``1.0e0`` as
``1.0`` and drop digits beyond double precision; the raw string the tool ran
with is unaffected.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any


#: Bidirectional formatting characters (Unicode UAX #9): they reorder how the
#: rest of a console or journal line is displayed. Other format characters
#: (ZWJ, ZWNJ, soft hyphen) are ordinary text and stay readable.
_BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def _is_unsafe_to_print(char: str) -> bool:
    """Whether *char* printed raw would act on a terminal or split a log line.

    C1 controls (``Cc``) drive terminals, bidi controls reorder the line, and
    the line/paragraph separators (``Zl``/``Zp``) break ``splitlines()``.
    """
    return unicodedata.category(char) in ("Cc", "Zl", "Zp") or char in _BIDI_CONTROLS


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def format_tool_arguments(arguments: str) -> str | None:
    """Return UTF-8-safe object JSON, or None to preserve the original string."""
    try:
        parsed = json.loads(arguments, object_pairs_hook=_unique_object)
        if not isinstance(parsed, dict):
            return None
        formatted = json.dumps(parsed, ensure_ascii=False, allow_nan=False)
        formatted.encode("utf-8", errors="strict")
        # json.dumps escapes only C0 controls; anything else decoded from a
        # \u escape would reach the journal and the terminal as a raw control.
        if any(_is_unsafe_to_print(char) for char in formatted):
            return None
        return formatted
    except (ValueError, RecursionError, TypeError):
        return None
