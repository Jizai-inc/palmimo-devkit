"""Headless CLI front end -- drives the companion agent over stdin/stdout.

No Textual, no terminal UI: one typed instruction per stdin line, and one
JSON line per :class:`~palmimo_companion_agent.pipeline.history.Event` on stdout
(JSONL, the same shape :mod:`palmimo_companion_agent.pipeline.event_log` writes
to a log file) -- so an external driver (a test harness, another agent) can
script and observe the companion agent without a terminal. A line starting
with ``/hear `` runs the rest through :meth:`~palmimo_companion_agent.pipeline.conductor.Conductor.hear`
instead of ``submit_user_text`` (see :func:`_parse_hear`); ``/exit`` ends the
session; any other line is a plain typed instruction.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
import threading
from typing import TYPE_CHECKING, TextIO

from ..event_log import emit_event
from ..wiring import Runtime, build_runtime


if TYPE_CHECKING:
    from ..settings import PipelineSettings


def _is_exit_command(text: str) -> bool:
    """Whether *text* is the ``/exit`` command exactly (matching the TUI's own rule)."""
    return text.strip() == "/exit"


def _parse_hear(text: str) -> tuple[bool, str | None]:
    """Parse a stdin line as the ``/hear`` command.

    Returns ``(is_hear, argument)`` -- ``is_hear`` alone decides whether
    *text* is the command at all. ``argument`` is None both when it isn't
    (``is_hear`` False) and when the command's own argument is empty
    (``is_hear`` True) -- the caller warns in that latter case rather than
    submitting empty speech.
    """
    if text == "/hear":
        return True, None
    if not text.startswith("/hear "):
        return False, None
    argument = text[len("/hear ") :].strip()
    return True, (argument or None)


async def _stdin_loop(runtime: Runtime, *, stdin: TextIO | None = None) -> None:
    """Read *stdin* (default: :data:`sys.stdin`) line by line, submitting each as an instruction.

    ``readline()`` blocks, so it runs on a daemon reader thread that bridges
    each line to an :class:`asyncio.Queue` via ``call_soon_threadsafe`` --
    the same shape as the wake-word example's mic subscription draining,
    adapted for stdin. EOF or ``/exit`` (stripped exactly) ends the loop; the
    reader thread is simply abandoned on the way out rather than joined.
    """
    src = stdin if stdin is not None else sys.stdin
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def _post(item: str | None) -> None:
        with contextlib.suppress(RuntimeError):  # the loop may already be closing at shutdown
            loop.call_soon_threadsafe(queue.put_nowait, item)

    def _reader() -> None:
        try:
            while True:
                line = src.readline()
                if not line:  # EOF
                    _post(None)
                    return
                _post(line)
        except Exception as exc:
            print(f"[stdin] reader failed: {exc!r}", file=sys.stderr, flush=True)
            _post(None)

    threading.Thread(target=_reader, name="companion-agent-stdin", daemon=True).start()

    while True:
        line = await queue.get()
        if line is None:
            return
        text = line.strip()
        if not text:
            continue
        if _is_exit_command(text):
            return
        try:
            is_hear, heard = _parse_hear(text)
            if is_hear and heard is None:
                print("[stdin] /hear requires text after it, e.g. '/hear hello there'", file=sys.stderr, flush=True)
            elif is_hear:
                assert heard is not None  # the is_hear-and-empty case was handled above
                await runtime.conductor.hear(heard)
            else:
                runtime.conductor.submit_user_text(text)
        except Exception as exc:  # a malformed line must not kill the session
            print(f"[stdin] failed to submit input: {exc!r}", file=sys.stderr, flush=True)


async def run_cli(settings: PipelineSettings) -> None:
    """Build the runtime and drive it headlessly until ``/exit``, EOF, or a signal.

    stdout carries JSONL history events exclusively; every log record goes to
    stderr instead. ``basicConfig`` runs here as well as at the console entry
    point, because this coroutine is also awaited directly by an external
    driver that never passes through that entry point -- without a root handler
    ``logging.lastResort`` would drop everything below ``WARNING``. It is a
    no-op once the entry point has run, so the two do not fight.

    This front end, and only this one, raises the agent's own logger and the
    SDK's to ``INFO``: those records are per utterance and per mic generation,
    which is what a headless run wants on stderr and what would scribble over
    the TUI's rendering. SIGTERM (and SIGINT, so Ctrl+C also goes through the
    same graceful path rather than raising ``KeyboardInterrupt`` mid-shutdown)
    cancel the stdin loop, which then lets
    :meth:`~palmimo_companion_agent.pipeline.wiring.Runtime.aclose` run in the
    ``finally`` block below.
    """
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    logging.getLogger("palmimo_companion_agent").setLevel(logging.INFO)
    logging.getLogger("palmimo_sdk").setLevel(logging.INFO)
    runtime = build_runtime(settings)
    runtime.history.subscribe(lambda event: emit_event(event, out=sys.stdout))
    await runtime.start()

    stdin_task = asyncio.ensure_future(_stdin_loop(runtime))
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):  # add_signal_handler isn't available on Windows
        loop.add_signal_handler(signal.SIGTERM, stdin_task.cancel)
        loop.add_signal_handler(signal.SIGINT, stdin_task.cancel)

    try:
        with contextlib.suppress(asyncio.CancelledError):
            await stdin_task
    finally:
        with contextlib.suppress(NotImplementedError):
            loop.remove_signal_handler(signal.SIGTERM)
            loop.remove_signal_handler(signal.SIGINT)
        await runtime.aclose()


__all__ = ["run_cli"]
