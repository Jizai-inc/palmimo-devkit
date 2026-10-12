"""Output behavior for redirected streams and terminals."""

from __future__ import annotations

import io
import sys

import pytest

from palmimo_companion_agent.output import configure_output


@pytest.mark.parametrize("destination", ["stdout", "stderr"])
@pytest.mark.parametrize("terminal", [False, True])
def test_configure_output_preserves_terminal_controls_and_escapes_redirected_output(
    destination: str,
    terminal: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stream(io.TextIOWrapper):
        def isatty(self) -> bool:
            return terminal

    buffer = io.BytesIO()
    stream = Stream(buffer, encoding="utf-8", errors="strict")
    monkeypatch.setattr(sys, destination, stream)
    configure_output()
    configure_output()
    text = "日本語\t\x1b\x00\x7f\u0085\u009f\u2028\u2029"
    if terminal:
        print(text, file=getattr(sys, destination), flush=True)
        assert buffer.getvalue().decode("utf-8") == text + "\n"
    else:
        print(text + "\ud800", file=getattr(sys, destination), flush=True)
        line = buffer.getvalue().decode("utf-8", errors="strict")
        assert line == "日本語\t" + r"\x1b\x00\x7f\x85\x9f\u2028\u2029\ud800" + "\n"
        assert len(line.splitlines()) == 1
