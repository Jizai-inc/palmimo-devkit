"""Process-wide output setup, applied at the console-script entry point.

Unlike the independent wake-word example, companion also escapes unsafe text
for journald and terminals.
"""

from __future__ import annotations

import contextlib
import io
import logging
import sys
from collections.abc import Iterable
from typing import Any, TextIO, cast


_ESCAPES = {
    code: rf"\x{code:02x}" if code <= 0xFF else rf"\u{code:04x}"
    for code in [*range(32), *range(0x7F, 0xA0), 0x2028, 0x2029, *range(0xD800, 0xE000)]
    if code not in (9, 10)
}


class _SafeOutput:
    """Escape unsafe text before forwarding output.

    Journald's JSON output makes fields containing U+007F through U+009F
    numeric arrays. ``splitlines()`` splits on U+0085 and U+2028/U+2029,
    and unpaired surrogates cannot be encoded as UTF-8. Escaping these and
    other controls keeps redirected output writable and on its original lines.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        terminal = getattr(stream, "isatty", lambda: False)()
        self._escapes = {code: value for code, value in _ESCAPES.items() if not terminal or code >= 0x80}

    def write(self, text: str) -> int:
        self._stream.write(text.translate(self._escapes))
        return len(text)

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def configure_output() -> None:
    """Line-buffer stdout, escape unsafe output, and log warnings to stderr.

    Terminal streams retain control sequences used by Textual. Logging stays
    at WARNING so library diagnostics do not overwhelm the UI or JSONL output.
    Streams substituted by a harness need not support reconfiguration.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None or isinstance(stream, _SafeOutput):
            continue
        if isinstance(stream, io.TextIOWrapper):
            with contextlib.suppress(ValueError):
                stream.reconfigure(
                    errors="backslashreplace",
                    line_buffering=True if name == "stdout" else stream.line_buffering,
                )
        setattr(sys, name, cast(TextIO, _SafeOutput(stream)))
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)


__all__ = ["configure_output"]
