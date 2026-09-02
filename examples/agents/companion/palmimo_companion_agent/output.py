"""Process-wide output setup, applied once at the console-script entry point.

Python block-buffers stdout whenever it is not a terminal, so anything written
without an explicit flush sits in the buffer until the process exits -- under a
redirect, or a service manager such as systemd or nohup, an agent that is
running normally looks exactly like a hung one.

Nothing this project puts on stdout today is at risk: the headless CLI's JSONL
history is flushed record by record by ``emit_event``, the realtime front end
passes ``flush=True`` at each of its own call sites, the TUI owns the terminal,
and every diagnostic goes to stderr. So this is a guarantee about the stream
rather than a fix for a symptom the companion has right now -- one that holds
for a call site added later, for anything the SDK or a user's own code prints,
and that keeps this example's behaviour identical to the wake-word one, where
the same buffering did hide an entire session.

Line buffering is set on the stream rather than by passing ``flush=True`` at
each call site precisely because the per-call-site version is a promise every
future line has to remember, and it cannot reach output produced inside the SDK
at all.

Deliberately duplicated in the wake-word example rather than shared: the two
example projects are independent, and neither may grow a dependency on the
other just to reuse a dozen lines.
"""

from __future__ import annotations

import contextlib
import io
import logging
import sys


def configure_output() -> None:
    """Make this process's output visible when stdout is not a terminal.

    Line-buffers stdout so output appears as it happens, and gives logging a
    stderr destination so log records stay off the stdout stream the headless
    CLI reserves for JSONL. The root logger stays at ``WARNING``: the LLM/HTTP
    libraries underneath are noisy at ``INFO``, and the mic problems an operator
    needs to see (dropped audio, device trouble) are logged by the SDK at
    ``WARNING`` already, so they arrive without raising anything.

    Runs for both front ends, so it deliberately raises no logger to ``INFO``.
    Under ``--ui tui`` any record written here lands on stderr in the middle of
    Textual's rendering -- including during shutdown, where the runtime closes
    before the app exits. A front end that has somewhere to put ``INFO`` turns
    it on for itself (see :func:`~palmimo_companion_agent.pipeline.ui.cli.run_cli`).

    Safe to call when stdout is not a real text stream (a stream replaced by a
    test harness, say): the line-buffering step is skipped rather than raising.
    """
    _line_buffer_stdout()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)


def _line_buffer_stdout() -> None:
    """Switch stdout to line buffering, when this process's stdout supports it."""
    # reconfigure() lives on TextIOWrapper, which is what a real process stdout
    # is; anything else here is a substitute installed by a harness, which needs
    # no reconfiguring anyway. The ValueError guard covers the remaining case of
    # a wrapper whose underlying buffer has been detached.
    if isinstance(sys.stdout, io.TextIOWrapper):
        with contextlib.suppress(ValueError):
            sys.stdout.reconfigure(line_buffering=True)


__all__ = ["configure_output"]
