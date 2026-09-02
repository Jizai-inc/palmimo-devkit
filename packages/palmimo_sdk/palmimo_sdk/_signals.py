# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Signal-suppression helper shared by the shutdown-ramp guards.

:func:`signals_ignored` is the one mechanism behind both
:func:`~palmimo_sdk.robot._deferred_interrupts` (SIGINT only, guarding
:meth:`~palmimo_sdk.robot.Palmimo.disconnect`'s park) and
:func:`~palmimo_sdk.mcp.__main__._shutdown_signals_ignored` (the wider
termination-signal set an MCP server's shutdown needs). Consolidated here so
the two callers share one tested implementation instead of drifting.
"""

from __future__ import annotations

import contextlib
import signal
import threading
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Iterator


@contextlib.contextmanager
def signals_ignored(*signals: signal.Signals) -> Iterator[None]:
    """Make each of *signals* a no-op (``SIG_IGN``) for the duration of the block.

    ``SIG_IGN`` rather than :func:`signal.pthread_sigmask`: a blocked signal
    stays pending and fires the moment it is unblocked, which would just move
    a mashed signal to right after the block; an ignored one is discarded
    outright, and unlike a Python-level no-op handler it never reaches the C
    trampoline, so an asyncio app using ``loop.add_signal_handler`` does not
    get a callback during the block either. A caller that genuinely must
    interrupt the block still has SIGKILL — which this cannot (and should
    not) intercept.

    A no-op off the main thread, where Python never delivers signals to begin
    with, and when :func:`signal.signal` refuses installation (``ValueError``,
    e.g. an interpreter that restricts handler changes): any of *signals*
    already switched to ``SIG_IGN`` for this call is restored first, so a
    partial failure never leaves some signals ignored and others not.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous: dict[signal.Signals, Any] = {}

    def restore() -> None:
        for sig, handler in previous.items():
            # None means the handler it replaced came from outside Python;
            # signal.signal() will not take None back, so the default is the
            # closest restoration available.
            signal.signal(sig, signal.SIG_DFL if handler is None else handler)

    installed = True
    try:
        for sig in signals:
            previous[sig] = signal.signal(sig, signal.SIG_IGN)
    except ValueError:
        installed = False
    # Both yields sit outside the except clause: one inside it would attach the
    # ValueError as the __context__ of anything the block raises.
    if not installed:
        restore()
        yield
        return
    try:
        yield
    finally:
        restore()
