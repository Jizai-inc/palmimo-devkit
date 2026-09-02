# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Watchdog-thread timeout helper shared by the serial-backed peripherals.

:class:`~palmimo_sdk.io.dynamixel.DynamixelDriver` (port open + motor
handshake + arming) and :class:`~palmimo_sdk.io.display.FaceDisplay` (port
open) both hand the first exchange with a device off to a third-party /
OS-level blocking call (``dynamixel_sdk``'s ``PortHandler.openPort`` + ping
retries, ``pyserial``'s ``Serial()`` constructor). Neither exposes a real
deadline for that FIRST exchange -- it is bounded only once a connection is
already established. On a dev machine with no robot attached (or the wrong
device on the matched port), that first exchange can block indefinitely
instead of failing fast.

:func:`run_with_timeout` runs the blocking call on a background thread and
simply stops waiting after *timeout* seconds, raising :class:`ProbeTimeoutError`
instead of hanging the caller. The background thread itself cannot be
force-killed if it is genuinely stuck (never returning) in an OS-level
read/open -- that case is NOT reclaimable, and the thread is abandoned until
the process exits. What CAN be reclaimed is the slower-but-not-stuck case: the
call was merely running long (a flaky retry, a slow bus) and eventually
returns after the caller already gave up and raised. Without
*on_late_result*, that late return would otherwise leave an opened-but-
untracked resource (a serial port, an armed servo bus) orphaned -- held open,
invisible to the caller's own rollback, and able to race a caller's retry for
the same port. Passing *on_late_result* lets the caller close that orphan the
moment the abandoned worker thread actually finishes, which matters
specifically for the retry-races-the-orphan case; it is no help at all
against a worker that is truly stuck.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable


logger = logging.getLogger(__name__)


class ProbeTimeoutError(TimeoutError):
    """Raised by :func:`run_with_timeout` when *func* exceeds its deadline.

    Callers catch this and re-raise their own device-specific error (naming
    the device and port being probed) -- this type carries no such detail
    itself, since :func:`run_with_timeout` has none to give.
    """


def run_with_timeout[T](
    func: Callable[[], T],
    timeout: float,
    *,
    on_late_result: Callable[[T], None] | None = None,
) -> T:
    """Run *func* on a background thread, waiting at most *timeout* seconds.

    Args:
        func: Zero-argument callable to run. Runs on a daemon thread so a
            timeout never blocks process exit.
        timeout: Seconds to wait before giving up on *func*.
        on_late_result: Called with *func*'s return value if that value ends up
            with no owner: either *func* finished successfully AFTER this
            function already raised :class:`ProbeTimeoutError` (called on the
            background thread), or the caller was interrupted while waiting and
            is abandoning a result that had already landed (called on the
            caller's thread, before the interrupt is re-raised). Use this
            to close/release whatever *func* opened (a serial port, an armed
            servo bus) so an abandoned-but-eventually-successful worker
            doesn't leak an orphaned, open resource past the failed call.
            Any exception *on_late_result* itself raises is logged and
            swallowed -- there is no caller left to propagate it to. A late
            FAILURE from *func* (no successful result to clean up) is simply
            logged at warning level instead of invoking this callback.

    Returns:
        Whatever *func* returned, if it finished within *timeout*.

    Raises:
        ProbeTimeoutError: *func* was still running after *timeout* seconds.
        BaseException: Whatever *func* itself raised, re-raised on the
            caller's thread, if it finished (unsuccessfully) within the
            deadline.
    """
    result: list[T] = []
    error: list[BaseException] = []
    # gave_up marks the caller's deadline expiry; the worker checks it after
    # func() returns to route its outcome (on time -> result/error, late ->
    # on_late_result / warning log). Both sides decide under decision_lock:
    # without it the worker could pass its check just as join() expires,
    # parking a successful result where nobody reads it -- exactly the
    # orphaned resource on_late_result exists to prevent.
    gave_up = threading.Event()
    decision_lock = threading.Lock()

    def _target() -> None:
        try:
            value = func()
        except BaseException as exc:
            with decision_lock:
                if gave_up.is_set():
                    logger.warning("A timed-out probe raised after its deadline (result discarded): %s", exc)
                else:
                    error.append(exc)
            return
        with decision_lock:
            late = gave_up.is_set()
            if not late:
                result.append(value)
        if late and on_late_result is not None:
            # Outside the lock: cleanup may be slow (closing a bus), and the
            # caller never touches the lock again after giving up.
            try:
                on_late_result(value)
            except Exception:
                logger.exception("on_late_result cleanup raised for a late-arriving probe result")

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    try:
        thread.join(timeout)
    except BaseException:
        # The caller is being torn down mid-wait -- a shutdown signal raising
        # KeyboardInterrupt straight through join() -- rather than waiting the
        # deadline out. The worker keeps running func() either way, so this is
        # the same abandoned-resource situation the timeout path below handles,
        # and it must be routed the same way: mark the result given-up so a
        # still-running worker hands its success to on_late_result, and clean
        # up directly if the worker already settled one. For the servo bus that
        # cleanup is what cuts torque; without it the bus finishes arming on the
        # worker thread, torque on, with nobody left holding a reference to it.
        with decision_lock:
            abandoned = result[0] if result else None
            if not (result or error):
                gave_up.set()
        if abandoned is not None and on_late_result is not None:
            with contextlib.suppress(Exception):
                on_late_result(abandoned)
        raise
    # A dead worker ALWAYS settled result/error under the lock first, so
    # "nothing settled" is exactly "the worker is still running func()" --
    # no is_alive() check needed (one would race the worker's own exit).
    with decision_lock:
        timed_out = not (result or error)
        if timed_out:
            gave_up.set()
    if timed_out:
        raise ProbeTimeoutError(f"Timed out after {timeout:.1f}s waiting for a response.")
    if error:
        raise error[0]
    return result[0]
