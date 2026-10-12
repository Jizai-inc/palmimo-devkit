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
import sys
import threading
from typing import TYPE_CHECKING, TextIO

from palmimo_sdk.shutdown import (
    TERMINATING_SIGNALS,
    StopRequest,
    loop_stop_on_signals,
    signals_ignored,
    stop_flag_on_signals,
)

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


def run_cli(settings: PipelineSettings, *, read_stdin: bool = True) -> None:
    """Run the CLI until EOF, ``/exit``, or a stop signal, then release torque.

    stdout carries JSONL history events; logging and park reports go to stderr.
    ``read_stdin=False`` keeps a service session alive even with stdin at EOF.
    """
    stop = StopRequest()
    try:
        # Runner must see the startup handler, so it does not install its own SIGINT handler.
        with signals_ignored(*TERMINATING_SIGNALS), contextlib.ExitStack() as startup:
            startup.enter_context(stop_flag_on_signals(stop))
            asyncio.run(_run_cli(settings, read_stdin=read_stdin, stop=stop, startup=startup))
    except KeyboardInterrupt:
        pass


async def _run_cli(
    settings: PipelineSettings, *, read_stdin: bool, stop: StopRequest, startup: contextlib.ExitStack
) -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    logging.getLogger("palmimo_companion_agent").setLevel(logging.INFO)
    logging.getLogger("palmimo_sdk").setLevel(logging.INFO)
    runtime = build_runtime(settings)
    try:
        if stop.is_set():
            print("stop requested before connect; skipping connection", file=sys.stderr, flush=True)
            return
        await runtime.connect()
        if stop.is_set():
            print("stop requested; connect finished, parking", file=sys.stderr, flush=True)
            return
        startup.close()
        runtime.history.subscribe(lambda event: emit_event(event, out=sys.stdout))
        own_task = asyncio.current_task()
        assert own_task is not None

        def end_session() -> None:
            own_task.cancel()

        with loop_stop_on_signals(stop, end_session):
            try:
                if stop.is_set():
                    return
                await runtime.start()
                session = asyncio.create_task(_stdin_loop(runtime) if read_stdin else asyncio.Event().wait())
                await session
            except asyncio.CancelledError:
                if not stop.is_set():
                    raise
    finally:
        try:
            startup.close()
        finally:
            await runtime.aclose(stop)


__all__ = ["run_cli"]
