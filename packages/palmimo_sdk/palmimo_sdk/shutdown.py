# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""The one stop path every entry point ends in, and the three ways a signal reaches it.

A program driving this robot has exactly one thing it must do when it is asked
to stop: reach :meth:`~palmimo_sdk.robot.Palmimo.disconnect`, which is what
cuts servo torque. Nothing else about a shutdown is safety-critical, and
nothing else in here is shared.

Getting *to* that call is the part that cannot be shared, because it depends
on the shape of the loop being stopped:

* A loop that **awaits while idle** can be stopped from inside the event loop:
  :func:`loop_stop_on_signals` (``loop.add_signal_handler``).
* A loop that **blocks synchronously** (a mic queue, a socket read) never
  reaches an ``await``, so an in-loop handler would sit unread:
  :func:`stop_flag_on_signals` sets a flag the loop polls between blocks.
* A program with **no loop of its own to poll** -- one that hands control to a
  third-party runner (Textual, uvicorn) and only wants its own ``finally``
  back -- converts the signal to an exception: :func:`interrupt_on_signals`.

All three record the request on one :class:`StopRequest`, and all three end at
:func:`park` / :func:`park_async`. See ``doc/explanation/shutdown.md`` for the
decision in prose, and for what each entry point in this repository picked.

Two rules the callers depend on, both learned the hard way:

* **A checkpoint after a long blocking call must read a flag set by the C-level
  handler** (:func:`stop_flag_on_signals`), never one set by a loop callback --
  the callback is queued behind whatever the ready queue already held, so the
  flag can still be false when the ``await`` resumes.
* **A teardown that has to reach a park must hold :func:`signals_ignored` over
  itself.** Handing the dispositions back before it -- which is what leaving a
  delivery context manager does -- puts SIGTERM/SIGHUP back on ``SIG_DFL``,
  which terminates without unwinding.

Nothing here can promise the servos end up released. ``SIGKILL`` and power
loss run no code at all, and a client or service manager that stops waiting
mid-park kills the process with torque still on -- :func:`park` is bounded
(seconds), not instant. What this module removes is the case where the code
that *would* have released torque never ran.
"""

from __future__ import annotations

import contextlib
import signal
import sys
import threading
import time
from typing import TYPE_CHECKING

from ._signals import signals_ignored as _ignore_signals


if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable, Iterator
    from types import FrameType
    from typing import TextIO

    from .robot import Palmimo


def _signals(*names: str) -> tuple[signal.Signals, ...]:
    """Look up signals by name, skipping the ones this platform does not have (Windows: SIGHUP)."""
    return tuple(getattr(signal, name) for name in names if hasattr(signal, name))


def _restore_disposition(signum: int, handler: object) -> None:
    """Put *handler* back for *signum*, mapping ``None`` to ``SIG_DFL``.

    :func:`signal.signal` returns ``None`` for a disposition installed from
    outside Python, and refuses ``None`` on the way back in -- so handing back
    what it gave raises :class:`TypeError`. ``SIG_DFL`` is the closest
    restoration available.

    That matters more than a tidy-up usually would, because of where these
    restores run. One is the ``finally`` of :func:`interrupt_on_signals`, which
    on the wake-word agent wraps the park: an exception there escapes
    :func:`park`. The other is inside :func:`stop_flag_on_signals`'s own signal
    handler, on the second-signal path -- it restores and then re-raises, so a
    ``TypeError`` there means the re-delivery never happens and the operator's
    way out of a wedged loop silently is not one.
    """
    signal.signal(signum, signal.SIG_DFL if handler is None else handler)  # type: ignore[arg-type]


#: Signals whose default disposition terminates the interpreter **without
#: unwinding** -- no ``finally`` runs, so nothing parks. SIGHUP is in here
#: because the robot is normally driven over ssh: a dropped session (or a
#: closed terminal window) delivers it, which makes it a routine stop rather
#: than an exotic one.
TERMINATING_SIGNALS: tuple[signal.Signals, ...] = _signals("SIGTERM", "SIGHUP")

#: Every signal that means "stop": :data:`TERMINATING_SIGNALS` plus SIGINT.
#: SIGINT already unwinds on its own (Python raises ``KeyboardInterrupt`` for
#: it), so it is deliberately absent above and present here.
STOP_SIGNALS: tuple[signal.Signals, ...] = (*TERMINATING_SIGNALS, signal.SIGINT)


class StopRequest:
    """Whether a stop was asked for, and how long ago.

    The one piece of shutdown state every entry point has, kept apart from the
    delivery mechanism that sets it (see the module docstring). A caller reads
    :meth:`is_set` to decide whether to keep going, and :meth:`seconds_since`
    to say afterwards how long the stop took -- which is how an operator learns
    that a stop was slow enough for something upstream to have killed the
    process part-way through the park.
    """

    def __init__(self) -> None:
        self._flag = threading.Event()
        self._at: float | None = None

    def request(self) -> None:
        """Record a stop request; a later one does not move the clock.

        Called from OS signal handlers, so it does nothing that could deadlock
        or re-enter: no I/O, no event-loop call, no lock this process holds
        anywhere else.
        """
        if self._at is None:
            self._at = time.monotonic()
        self._flag.set()

    def is_set(self) -> bool:
        """Whether a stop has been requested. Usable as a bare predicate (``stop.is_set``)."""
        return self._flag.is_set()

    def seconds_since(self) -> float | None:
        """Seconds since the first request, or ``None`` if none arrived."""
        return None if self._at is None else time.monotonic() - self._at


@contextlib.contextmanager
def signals_ignored(*signums: signal.Signals) -> Iterator[None]:
    """Discard *signums* (default: :data:`STOP_SIGNALS`) for the duration of the block.

    Wraps the park, the one stretch that must run to completion: the call that
    cuts torque is the *last* statement of
    :meth:`~palmimo_sdk.robot.Palmimo.disconnect`, so a signal landing part-way
    through skips the torque-off entirely. That is not hypothetical -- an MCP
    stdio client sends SIGTERM 2.0s after closing stdin, and the park is a
    ~2.5s neck ramp plus the leg return, so on an ordinary client disconnect
    the signal lands *during* the park every time.

    ``SIG_IGN`` rather than :func:`signal.pthread_sigmask`: a blocked signal
    stays pending and fires the moment it is unblocked, which would just move
    the kill to immediately after the park; an ignored one is discarded. The
    park is bounded, and a caller that genuinely must stop it still has
    SIGKILL -- which this cannot, and should not, intercept.

    Off the main thread :func:`signal.signal` is not allowed, so this yields
    without installing anything. That is not a silent downgrade: Python only
    ever runs handlers on the main thread, so a park running in a worker thread
    is already immune to being unwound by one -- see :func:`park_async`, which
    is how an event loop gets both halves.

    The mechanism is :func:`palmimo_sdk._signals.signals_ignored`, which this
    only gives a default signal set to. There is one implementation on purpose:
    a second one here drifted from it, and because :func:`park` is what runs it,
    the drift was reachable from the one call whose whole job is to guarantee
    the torque-off. Two things it does that a plain install/restore pair does
    not, both of which :func:`park` depends on:

    - it installs inside a ``try``, and on a :class:`ValueError` (an interpreter
      that refuses handler changes) restores the ones it already switched. A
      partial install that lost its record would leave SIGTERM and SIGHUP
      ignored for the life of the process -- they would stop the robot never.
    - it maps a ``None`` previous handler (one that came from outside Python)
      to ``SIG_DFL`` on the way out. ``signal.signal`` refuses ``None``, and
      that ``TypeError`` would come out of the ``finally``, i.e. out of
      :func:`park`, which promises it does not raise.
    """
    with _ignore_signals(*(signums or STOP_SIGNALS)):
        yield


@contextlib.contextmanager
def interrupt_on_signals(stop: StopRequest, *signums: signal.Signals) -> Iterator[None]:
    """Make *signums* (default: :data:`TERMINATING_SIGNALS`) raise ``KeyboardInterrupt``.

    For a program that has handed control to something else -- Textual's app
    loop, uvicorn -- and only needs its own ``finally`` to run. Raising reuses
    the unwind Ctrl-C already gets rather than adding a second shutdown route
    to keep in step with the first.

    SIGINT is not in the default set because Python already delivers it exactly
    this way.

    This is deliberately NOT equivalent to what asyncio does with SIGINT inside
    a running loop: asyncio installs a cooperative cancellation, which lets an
    in-flight call drain, whereas an exception raised from a handler unwinds at
    whatever bytecode happened to be executing. Draining in-flight work on a
    termination signal needs the cooperative path, i.e.
    :func:`loop_stop_on_signals`.

    Off the main thread :func:`signal.signal` cannot install anything, so this
    degrades to the platform default -- a termination signal kills the process
    torque-enabled. The degradation is announced on stderr rather than passed
    off as supported.
    """

    def _raise(signum: int, frame: FrameType | None) -> None:
        # Timestamped before raising, so a caller measuring the shutdown
        # measures it from the signal, not from wherever the unwind landed.
        stop.request()
        raise KeyboardInterrupt

    if threading.current_thread() is not threading.main_thread():
        print(
            "WARNING: not running on the main thread -- cannot install shutdown signal handlers. "
            "A termination signal (SIGTERM/SIGHUP) will kill this process without releasing servo "
            "torque; the servos will stay energised and heat up.",
            file=sys.stderr,
        )
        yield
        return
    # Installed one at a time inside the `try`: if a later `signal.signal`
    # rejects a signum, the `finally` still hands back the ones already
    # switched, instead of leaving SIGTERM converted for the process's life.
    previous: dict[int, object] = {}
    try:
        for sig in signums or TERMINATING_SIGNALS:
            previous[sig] = signal.signal(sig, _raise)
        yield
    finally:
        for num, handler in previous.items():
            _restore_disposition(num, handler)


@contextlib.contextmanager
def stop_flag_on_signals(stop: StopRequest, *signums: signal.Signals) -> Iterator[Callable[[], bool]]:
    """Make *signums* (default: :data:`STOP_SIGNALS`) set a flag the caller polls.

    For a loop that blocks synchronously -- a mic queue, a blocking read --
    where neither an in-loop handler nor an exception is usable. An exception
    is not: the handler runs between bytecodes in whatever the main thread was
    doing, so anything it touches must be re-entrant, and unwinding from an
    arbitrary point loses the loop's own idea of where it is safe to stop. So
    the handler sets a flag and nothing else; the loop decides when to look.

    Installing a handler at all is also what keeps SIGINT usable here:
    :class:`asyncio.Runner` (which :func:`asyncio.run` uses) installs its own
    SIGINT handler whose first invocation only calls ``task.cancel()``, and a
    cancellation is delivered at an ``await`` that a synchronously blocked loop
    never reaches. With a non-default disposition already in place, ``Runner``
    leaves SIGINT alone.

    The flag is set by the C-level handler itself, so it is visible the instant
    the interrupted call returns -- there is no queue to drain first. That is
    what makes this the right shape for a checkpoint after a long blocking call
    (see :func:`loop_stop_on_signals` for the shape that does have that problem).

    A **second** signal hands every disposition back and re-delivers itself, so
    the default path applies. That is the way out of the case the flag cannot
    cover: a loop blocked on input that has stopped arriving entirely (a mic
    unplugged mid-run) never reaches the check.

    "Second" is counted *here*, not on *stop*. A ``StopRequest`` may be shared
    with an earlier phase that already recorded a stop -- and reading it would
    then make the operator's very first signal take the hand-back branch and
    terminate the process before the loop had a chance to break.

    Yields:
        A predicate that becomes ``True`` once one of *signums* has arrived.

    Raises:
        ValueError: If called off the main thread, where :func:`signal.signal`
            cannot install a handler.
    """
    previous: dict[int, object] = {}
    seen = threading.Event()

    def _restore() -> None:
        while previous:
            num, handler = previous.popitem()
            _restore_disposition(num, handler)

    def _handler(signum: int, frame: FrameType | None) -> None:
        if seen.is_set():  # second signal: the graceful path did not get us out
            _restore()
            signal.raise_signal(signum)
            return
        seen.set()
        stop.request()

    try:
        # Inside the `try` for the same reason as `interrupt_on_signals`: a
        # rejected signum part-way through would otherwise leave a handler
        # installed that sets a flag nobody is left to read.
        for num in signums or STOP_SIGNALS:
            previous[num] = signal.signal(num, _handler)
        yield stop.is_set
    finally:
        _restore()


@contextlib.contextmanager
def loop_stop_on_signals(stop: StopRequest, on_stop: Callable[[], None], *signums: signal.Signals) -> Iterator[None]:
    """Route *signums* (default: :data:`STOP_SIGNALS`) into the running event loop.

    For a loop that awaits while idle, which is the only shape where the stop
    can be delivered cooperatively: the handler runs as an ordinary callback
    *in* the loop, so *on_stop* may do real work -- cancel the task that is
    waiting, set an :class:`asyncio.Event` the session watches.

    Setting a flag alone is not enough for these callers even though they have
    a loop: their services block on a socket, a camera queue or a mic read
    rather than polling one, so a flag would sit unread until the session's own
    time limit elapsed -- past a service manager's stop timeout, which then
    SIGKILLs the process with the park unrun.

    **Delivery here is not immediate, and the caller must not treat it as
    such.** The handler becomes an ordinary loop callback, queued behind
    whatever was already in the ready queue -- so a flag read straight after the
    ``await`` this interrupted can still be false. Do not use this shape for a
    checkpoint after a long blocking call; use :func:`stop_flag_on_signals`,
    whose flag is set by the C-level handler and is therefore true the moment
    the call returns. This shape is for *doing* something in the loop (cancel
    the waiting task, set the event the session watches), not for polling.

    Handlers are removed on the way out. Installing one for SIGINT replaces the
    ``KeyboardInterrupt`` disposition, so leaving it in place through a teardown
    that itself takes seconds would swallow the operator's second and third
    Ctrl+C -- exactly when a misbehaving robot needs to be killed.

    What was installed underneath is put back by hand, because
    ``remove_signal_handler`` does not do it: it resets the disposition to
    ``SIG_DFL`` regardless of what it replaced. So leaving this block restores
    what was there -- an enclosing :func:`interrupt_on_signals` handler, say --
    and not ``SIG_DFL``.

    The caveat is about what *was* there, not about this restore. Usually
    nothing had installed a handler, so what comes back IS ``SIG_DFL``, and on
    SIGTERM/SIGHUP that terminates without unwinding. A teardown that runs
    after this block and has to reach a park must therefore hold
    :func:`signals_ignored` over itself rather than assume what it restored is
    safe to be interrupted by.

    ``add_signal_handler`` is not available on Windows. Nothing is installed
    there -- and nothing is restored either, since restoring a disposition this
    never replaced would silently revert whatever the block itself installed.
    """
    loop: asyncio.AbstractEventLoop = _running_loop()

    def _deliver() -> None:
        stop.request()
        on_stop()

    previous: dict[signal.Signals, object] = {}
    for sig in signums or STOP_SIGNALS:
        # Recorded only once the install has actually happened: on Windows
        # add_signal_handler raises, and a `previous` entry for a signal this
        # never touched would have the `finally` overwrite a live disposition.
        found = signal.getsignal(sig)
        try:
            loop.add_signal_handler(sig, _deliver)
        except NotImplementedError:
            continue
        previous[sig] = found
    try:
        yield
    finally:
        for sig, handler in previous.items():
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
            # None means the handler was not installed from Python (so there is
            # nothing to give back, and signal.signal would raise on it).
            if handler is not None:
                signal.signal(sig, handler)  # type: ignore[arg-type]


def _running_loop() -> asyncio.AbstractEventLoop:
    """Import asyncio lazily -- this module is imported by callers that never touch it."""
    import asyncio

    return asyncio.get_running_loop()


def park(robot: Palmimo, *, legs: bool = True, out: TextIO | None = None) -> None:
    """Release servo torque, uninterruptibly, and say on *out* whether it completed.

    This is where every stop path in this repository ends. It is safe to call
    more than once and safe to call on a robot that was never connected:
    :meth:`~palmimo_sdk.robot.Palmimo.disconnect` is idempotent and skips the
    park when there is nothing energised, so an entry point may put this in a
    ``finally`` without first working out whether an earlier teardown already
    ran.

    **It does not raise**, which is the other half of what a ``finally`` needs:
    neither the interrupt that ``disconnect`` re-raises after reaching the
    torque-off, nor an ``Exception`` from the driver, comes back out of here to
    replace a clean exit -- or the error that started the shutdown -- with a
    traceback from the teardown.

    *legs* false skips only the leg return to neutral, keeping the neck
    soft-release and the torque-off -- for a robot that may already be in a bad
    state, where streaming more motion commands could make it worse.

    The report lines are the only evidence a hardware run gets that the
    torque-off actually completed: without them a park cut short is
    indistinguishable from a clean shutdown, since callers suppress the
    interrupt and exit 0 either way. The completion line is deliberately after
    the call, so a partial park cannot be reported as success -- and a failure
    that was held rather than raised replaces it with a line saying torque may
    still be on, because a swallowed driver error would otherwise read as a
    successful park.
    """
    stream = sys.stderr if out is None else out
    # The ignore covers the whole body, not just the disconnect. This is
    # normally entered from a `finally` after the first Ctrl+C, so a second one
    # -- or a SIGTERM under `interrupt_on_signals` -- landing on the checks or
    # the report line would unwind before the torque-off, which is the case
    # this module exists to close. A blocked stderr pipe widens that window
    # arbitrarily.
    with signals_ignored():
        if not robot.has_connectable_resource:
            return
        parking = robot.is_connected
        if parking:
            print("parking the robot -- easing to neutral and releasing servo torque...", file=stream)
        # An `Exception` is held: every entry point calls this from a
        # `finally`, so letting one out would replace a clean exit -- or the
        # error that started the shutdown -- with a traceback from the
        # teardown. But it is never swallowed silently: torque may still be on,
        # which is the one fact an operator has to be told, so the failure is
        # reported in place of the line that would otherwise claim success.
        failure: BaseException | None = None
        try:
            robot.disconnect(park=legs)
        except BaseException as exc:  # reported below, never re-raised out of a caller's finally
            failure = exc
        # disconnect() re-raises an interrupt that landed mid-teardown, but only
        # after reaching the torque-off: that is the shutdown being asked for,
        # and reporting it would tell an operator to unplug a robot already
        # released. The test is the robot's own state, not the exception's type
        # -- a BaseException raised *by* the driver disconnect leaves torque on
        # and still has to be reported.
        if failure is not None and not isinstance(failure, Exception) and not robot.is_connected:
            failure = None
        if failure is not None:
            print(
                f"PARK FAILED -- servo torque may still be on ({type(failure).__name__}: {failure}). "
                "Unplug the AC adapter before handling the robot.",
                file=stream,
            )
        elif parking:
            print("park complete -- servo torque released", file=stream)


async def park_async(robot: Palmimo, *, legs: bool = True, out: TextIO | None = None) -> None:
    """:func:`park`, for a caller that must not block its event loop for the duration.

    The park is seconds of blocking hardware I/O, so it runs in a worker
    thread -- but :func:`signals_ignored` only works on the main thread, so the
    ignore is installed *here*, on the loop's own thread, around the wait. The
    two halves cover each other: Python runs signal handlers on the main thread
    only, so a signal cannot unwind the parking thread, and the ignore stops it
    unwinding the loop that is waiting on it.

    A cancellation of *this* coroutine does not end the wait, and that is the
    point. The thread cannot be cancelled, so returning early would drop the
    ignore and hand control back while the neck ramp is still mid-flight --
    ``asyncio.run``'s own task cancellation at teardown does exactly that, and
    the next signal would then land on a restored disposition with torque still
    on. So the cancellation is absorbed and re-awaited until the park is
    genuinely finished; anything that must stop it sooner has SIGKILL.
    """
    import asyncio
    import functools

    # run_in_executor returns a plain Future, deliberately: `asyncio.to_thread`
    # wraps one in a Task, and `asyncio.run`'s teardown cancels every Task in
    # `all_tasks()` directly. `shield` does not help there -- it protects the
    # awaiting coroutine, not the awaited Task -- so the wait below would end
    # while the worker thread was still in the neck ramp, drop the ignore, and
    # leave the next signal landing on a restored disposition with torque on.
    # A Future is not in `all_tasks()`, so nothing cancels it; `asyncio.run`
    # then joins the thread in `shutdown_default_executor()`.
    future = asyncio.get_running_loop().run_in_executor(None, functools.partial(park, robot, legs=legs, out=out))
    with signals_ignored():
        while not future.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(future)


__all__ = [
    "STOP_SIGNALS",
    "TERMINATING_SIGNALS",
    "StopRequest",
    "interrupt_on_signals",
    "loop_stop_on_signals",
    "park",
    "park_async",
    "signals_ignored",
    "stop_flag_on_signals",
]
