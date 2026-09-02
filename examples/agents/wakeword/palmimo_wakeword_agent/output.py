"""Process-wide output setup, applied once at the console-script entry point.

Python block-buffers stdout whenever it is not a terminal, and nothing here
flushes it, so ``palmimo-wakeword-agent > log`` (or the same run under a
service manager such as systemd or nohup) leaves the log empty while the agent
is in fact running normally: the startup banner and the ``[heard]`` lines exist
from the start, they just sit in the buffer until the process exits. That
looks exactly like a hung agent.

Line buffering is set once, on the stream, rather than by passing
``flush=True`` at each call site: a call site added later would silently
reintroduce the problem, and ``flush=True`` cannot reach output produced inside
the SDK at all.

Deliberately duplicated in the companion example rather than shared: the two
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

    Line-buffers stdout so progress lines appear as they happen, and gives
    logging a stderr destination so log records never interleave with whatever
    stdout carries. The root logger stays at ``WARNING`` -- the LLM/HTTP
    libraries underneath are noisy at ``INFO``, and the mic trouble an operator
    has to see (input overflows, read failures) is logged by the SDK at
    ``WARNING`` already, so it arrives without raising anything. ``palmimo_sdk``
    is raised to ``INFO`` for one record on top of that: the capture summary
    ``MicStream`` logs when a capture generation ends, which is what says how
    close to the limit the capture actually ran.

    Safe to call when stdout is not a real text stream (a stream replaced by a
    test harness, say): the line-buffering step is skipped rather than raising.
    """
    _line_buffer_stdout()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    logging.getLogger("palmimo_sdk").setLevel(logging.INFO)


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
