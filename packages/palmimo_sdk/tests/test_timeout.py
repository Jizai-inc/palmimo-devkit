"""run_with_timeout — the watchdog-thread deadline + late-result cleanup contract
shared by DynamixelDriver.connect() and FaceDisplay.connect() (see io/_timeout.py).
"""

import logging
import threading
import time

import pytest

from palmimo_sdk.io._timeout import ProbeTimeoutError, run_with_timeout


def test_returns_promptly_when_func_finishes_in_time() -> None:
    """A func that returns well within the deadline is returned unchanged."""
    assert run_with_timeout(lambda: 42, timeout=1.0) == 42


def test_raises_probe_timeout_error_when_func_never_returns() -> None:
    """A func blocked past the deadline raises ProbeTimeoutError promptly, not after
    hanging for however long func itself would have taken."""
    never_return = threading.Event()  # never set -> func.wait() blocks "forever"

    start = time.perf_counter()
    with pytest.raises(ProbeTimeoutError):
        run_with_timeout(never_return.wait, timeout=0.05)
    elapsed = time.perf_counter() - start

    assert elapsed < 2.0, "run_with_timeout should fail fast, not hang"


def test_propagates_funcs_own_exception_when_it_fails_in_time() -> None:
    """A func that raises before the deadline has its own exception re-raised as-is."""

    def boom() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_with_timeout(boom, timeout=1.0)


def test_late_success_invokes_on_late_result_once_worker_unblocks() -> None:
    """Late success: a func that opens/creates a resource and then blocks past
    the deadline, but eventually returns successfully (a flaky retry that was merely
    slow, not stuck), must not leak that resource. on_late_result is invoked with the
    late result once the abandoned worker actually finishes, so the caller can close it."""
    release = threading.Event()
    late_result_seen: list[int] = []
    late_result_ready = threading.Event()

    def slow_success() -> int:
        release.wait()  # blocks past the deadline until the test unblocks it
        return 7

    def on_late(value: int) -> None:
        late_result_seen.append(value)
        late_result_ready.set()

    start = time.perf_counter()
    with pytest.raises(ProbeTimeoutError):
        run_with_timeout(slow_success, timeout=0.05, on_late_result=on_late)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, "run_with_timeout should fail fast, not wait for the worker"
    assert late_result_seen == []  # not yet -- the worker is still blocked

    release.set()  # let the abandoned worker finish
    assert late_result_ready.wait(timeout=2.0), "on_late_result was never called"
    assert late_result_seen == [7]


def test_late_result_without_callback_is_silently_dropped() -> None:
    """Without on_late_result, a late success is simply discarded -- no crash, no callback."""
    release = threading.Event()

    def slow_success() -> int:
        release.wait()
        return 7

    with pytest.raises(ProbeTimeoutError):
        run_with_timeout(slow_success, timeout=0.05)

    release.set()
    time.sleep(0.1)  # give the abandoned worker a moment to finish; nothing to assert on


def test_late_failure_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    """A func that raises AFTER the deadline already fired is logged at warning level
    instead of vanishing or crashing anything -- there is no caller left to raise to."""
    release = threading.Event()

    def slow_failure() -> None:
        release.wait()
        raise RuntimeError("late boom")

    with caplog.at_level(logging.WARNING, logger="palmimo_sdk.io._timeout"):
        with pytest.raises(ProbeTimeoutError):
            run_with_timeout(slow_failure, timeout=0.05)
        release.set()
        deadline = time.perf_counter() + 2.0
        while not caplog.records and time.perf_counter() < deadline:
            time.sleep(0.01)

    assert any("late boom" in r.message for r in caplog.records)


def test_on_late_result_exception_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    """A cleanup callback that itself raises is logged and swallowed -- it must never
    crash the abandoned worker thread (there's no caller left to propagate it to)."""
    release = threading.Event()

    def slow_success() -> int:
        release.wait()
        return 1

    def flaky_cleanup(_value: int) -> None:
        raise RuntimeError("cleanup boom")

    with caplog.at_level(logging.ERROR, logger="palmimo_sdk.io._timeout"):
        with pytest.raises(ProbeTimeoutError):
            run_with_timeout(slow_success, timeout=0.05, on_late_result=flaky_cleanup)
        release.set()
        deadline = time.perf_counter() + 2.0
        while not caplog.records and time.perf_counter() < deadline:
            time.sleep(0.01)

    assert any("cleanup boom" in (r.message + str(r.exc_info)) for r in caplog.records)


# ----------------------------------------------------------------------
# Caller interrupted mid-wait
#
# join() is interruptible: a shutdown signal raises KeyboardInterrupt straight
# through it. The worker keeps running func() regardless, so the result it
# eventually produces has no owner -- the same situation as the deadline
# expiring, and it must be cleaned up the same way. For DynamixelDriver that
# result is a bus whose connect already enabled torque on every servo.
# ----------------------------------------------------------------------


def test_caller_interrupted_while_worker_runs_routes_the_late_result_to_the_callback() -> None:
    """An interrupt while func() is still running must still hand the result to on_late_result."""
    release = threading.Event()
    closed: list[str] = []

    class InterruptingJoinThread(threading.Thread):
        def join(self, timeout: float | None = None) -> None:
            raise KeyboardInterrupt

    original_thread = threading.Thread
    threading.Thread = InterruptingJoinThread  # type: ignore[misc]
    try:
        with pytest.raises(KeyboardInterrupt):
            run_with_timeout(lambda: (release.wait(10), "armed-bus")[1], timeout=5.0, on_late_result=closed.append)
    finally:
        threading.Thread = original_thread  # type: ignore[misc]

    release.set()
    deadline = time.perf_counter() + 5.0
    while not closed and time.perf_counter() < deadline:
        time.sleep(0.01)

    assert closed == ["armed-bus"], "an abandoned worker's result was leaked instead of being closed"


def test_caller_interrupted_after_the_result_landed_closes_it_on_the_callers_thread() -> None:
    """The interrupt can also land just after join() returns, with the result already settled."""
    closed: list[str] = []

    class LateInterruptThread(threading.Thread):
        def join(self, timeout: float | None = None) -> None:
            super().join(timeout)
            raise KeyboardInterrupt

    original_thread = threading.Thread
    threading.Thread = LateInterruptThread  # type: ignore[misc]
    try:
        with pytest.raises(KeyboardInterrupt):
            run_with_timeout(lambda: "armed-bus", timeout=5.0, on_late_result=closed.append)
    finally:
        threading.Thread = original_thread  # type: ignore[misc]

    assert closed == ["armed-bus"], "a settled result the caller abandoned was leaked instead of being closed"
