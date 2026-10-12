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
        print(text + "\ud83d", file=getattr(sys, destination), flush=True)
        assert buffer.getvalue().decode("utf-8") == "日本語\t\x1b\x00\x7f" + r"\x85\x9f\u2028\u2029\ud83d" + "\n"
    else:
        print(text + "\ud800", file=getattr(sys, destination), flush=True)
        line = buffer.getvalue().decode("utf-8", errors="strict")
        assert line == "日本語\t" + r"\x1b\x00\x7f\x85\x9f\u2028\u2029\ud800" + "\n"
        assert len(line.splitlines()) == 1


@pytest.mark.parametrize("destination", ["stdout", "stderr"])
def test_configure_output_leaves_absent_stream_unwrapped(
    destination: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, destination, None)
    configure_output()
    assert getattr(sys, destination) is None


@pytest.mark.parametrize("destination", ["stdout", "stderr"])
def test_configure_output_escapes_terminal_controls_in_substitute_stream(
    destination: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stream(io.StringIO):
        def isatty(self) -> bool:
            return True

    stream = Stream()
    monkeypatch.setattr(sys, destination, stream)
    configure_output()
    print("日本語\x1b\u009b\u2028\ud83d", file=getattr(sys, destination))
    assert stream.getvalue() == "日本語\x1b" + r"\x9b\u2028\ud83d" + "\n"
