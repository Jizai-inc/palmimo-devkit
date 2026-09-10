"""palmimo_sdk.shutdown — the shared stop path, and the three ways a signal reaches it."""

import asyncio
import contextlib
import signal
import threading
import time
from typing import Any, cast

import pytest

from palmimo_sdk import HeadCamera, Microphone, Palmimo, ServoDriver
from palmimo_sdk.shutdown import (
    STOP_SIGNALS,
    TERMINATING_SIGNALS,
    StopRequest,
    interrupt_on_signals,
    loop_stop_on_signals,
    park,
    park_async,
    signals_ignored,
    stop_flag_on_signals,
)


@pytest.fixture
def sigterm_sentinel() -> Any:
    """Install a distinguishable SIGTERM handler and restore it afterwards.

    Every test here manipulates the process-wide disposition, so each one needs
    a known handler to assert was put back -- and the suite needs SIGTERM left
    the way pytest found it.
    """

    def sentinel(signum: int, frame: Any) -> None:  # pragma: no cover -- never invoked
        raise AssertionError("the sentinel handler should never run")

    previous = signal.signal(signal.SIGTERM, sentinel)
    try:
        yield sentinel
    finally:
        signal.signal(signal.SIGTERM, previous)


class FakeDriver:
    """Servo driver double that records the one call that actually cuts torque."""

    def __init__(self) -> None:
        self.is_connected = False
        self.disconnected = False

    def connect(self) -> None:
        self.is_connected = True

    def disconnect(self) -> None:
        self.is_connected = False
        self.disconnected = True


class _SlowDriver(FakeDriver):
    """A driver whose torque-off takes long enough to outlive the loop's teardown."""

    def disconnect(self) -> None:
        time.sleep(0.2)
        super().disconnect()


class InterruptingPeripheral:
    """Mic/camera double whose ``close()`` raises, as a mashed Ctrl+C would."""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.closed = False

    def open(self) -> None:
        pass

    def start_drain(self) -> None:
        """Only the camera is asked for this; a no-op keeps one double for both."""

    def close(self) -> None:
        self.closed = True
        raise self._error


def _robot(driver: FakeDriver, *, mic: Any = None, camera: Any = None) -> Palmimo:
    """Build a connected-capable facade from doubles, casting at the one boundary that needs it."""
    return Palmimo(
        driver=cast("ServoDriver", driver),
        mic=cast("Microphone | None", mic),
        camera=cast("HeadCamera | None", camera),
        auto_wake=False,
    )


# ----------------------------------------------------------------------
# StopRequest: the one piece of state every entry point shares
# ----------------------------------------------------------------------


def test_stop_request_is_not_set_before_anything_asks_it_to_stop() -> None:
    stop = StopRequest()

    assert stop.is_set() is False
    assert stop.seconds_since() is None


def test_stop_request_clock_starts_at_the_first_request_not_the_last(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repeat signal must not restart the clock and hide an overrun.

    A second signal can land while the unwind is still on its way to the park;
    measuring from that one would subtract exactly the time that made the
    shutdown late. The clock is driven rather than slept out, so the two
    readings differ by more than the platform's timer resolution.
    """
    import palmimo_sdk.shutdown as shutdown_module

    now = [100.0]
    monkeypatch.setattr(shutdown_module.time, "monotonic", lambda: now[0])
    stop = StopRequest()

    stop.request()
    now[0] = 105.0
    stop.request()
    now[0] = 110.0

    assert stop.seconds_since() == 10.0, "the repeat request restarted the shutdown clock"


# ----------------------------------------------------------------------
# signals_ignored: the park is the one stretch that must run to completion
# ----------------------------------------------------------------------


def test_signals_ignored_discards_the_signal_and_restores_the_handler(sigterm_sentinel: Any) -> None:
    """SIG_IGN must drop the signal outright, not defer it to the moment the block exits.

    A blocked signal (pthread_sigmask) would stay pending and fire on unblock,
    which would only move the kill to just after the park.
    """
    with signals_ignored():
        signal.raise_signal(signal.SIGTERM)

    assert signal.getsignal(signal.SIGTERM) is sigterm_sentinel


def test_signals_ignored_covers_sigint_so_a_mashed_ctrl_c_cannot_skip_the_torque_off() -> None:
    """Ctrl-C during the park is the other way to escape the unsuppressed driver disconnect.

    Wider than the raise-set on purpose: SIGINT does not need converting into a
    KeyboardInterrupt (Python already does that), but it does need silencing
    once the park has started.
    """
    previous = signal.getsignal(signal.SIGINT)
    try:
        with signals_ignored():
            signal.raise_signal(signal.SIGINT)
    finally:
        signal.signal(signal.SIGINT, previous)

    assert signal.getsignal(signal.SIGINT) is previous


def test_signals_ignored_installs_nothing_off_the_main_thread() -> None:
    """signal.signal() raises off the main thread; the park must still run there.

    park_async() relies on this: it installs the ignore on the loop's thread and
    runs the park in a worker, where Python never delivers a handler anyway.
    """
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            with signals_ignored():
                pass
            outcome.append(None)
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()

    assert outcome == [None]


# ----------------------------------------------------------------------
# Delivery shape 1: convert to an exception (no loop of our own)
# ----------------------------------------------------------------------


def test_interrupt_on_signals_turns_sigterm_into_a_keyboard_interrupt(sigterm_sentinel: Any) -> None:
    stop = StopRequest()

    with pytest.raises(KeyboardInterrupt), interrupt_on_signals(stop):
        signal.raise_signal(signal.SIGTERM)

    assert stop.is_set(), "the handler raised without recording that a stop had been asked for"


def test_interrupt_on_signals_restores_the_handler_it_replaced(sigterm_sentinel: Any) -> None:
    """main() is an ordinary callable the suite runs in-process; it must not leak a disposition."""
    with contextlib.suppress(KeyboardInterrupt), interrupt_on_signals(StopRequest()):
        signal.raise_signal(signal.SIGTERM)

    assert signal.getsignal(signal.SIGTERM) is sigterm_sentinel


def test_interrupt_on_signals_leaves_sigint_alone() -> None:
    """Python already delivers SIGINT as a KeyboardInterrupt -- installing a second route is churn."""
    assert signal.SIGINT not in TERMINATING_SIGNALS

    previous = signal.getsignal(signal.SIGINT)
    with interrupt_on_signals(StopRequest()):
        assert signal.getsignal(signal.SIGINT) is previous


# ----------------------------------------------------------------------
# Delivery shape 2: set a flag a synchronously blocked loop polls
# ----------------------------------------------------------------------


def test_stop_flag_on_signals_flips_the_predicate_without_raising(sigterm_sentinel: Any) -> None:
    """The handler runs between bytecodes, so it must do nothing that can re-enter."""
    stop = StopRequest()

    with stop_flag_on_signals(stop) as should_stop:
        assert should_stop() is False
        signal.raise_signal(signal.SIGTERM)
        assert should_stop() is True


def test_stop_flag_on_signals_hands_the_disposition_back_on_the_second_signal(
    sigterm_sentinel: Any,
) -> None:
    """The way out when the flag is never read: a loop blocked on input that stopped arriving.

    The second signal restores what was installed underneath and re-delivers
    itself, so the default path applies -- here, the sentinel handler.
    """
    stop = StopRequest()
    reached: list[str] = []

    def sentinel(signum: int, frame: Any) -> None:
        reached.append("default")

    signal.signal(signal.SIGTERM, sentinel)
    with stop_flag_on_signals(stop):
        signal.raise_signal(signal.SIGTERM)
        signal.raise_signal(signal.SIGTERM)

    assert reached == ["default"], "the second signal did not reach the disposition underneath"


def test_stop_flag_on_signals_covers_sigterm_and_sighup_as_well_as_sigint() -> None:
    """SIGINT alone is what the agent used to honour; SIGTERM is what users reach for instead."""
    assert signal.SIGINT in STOP_SIGNALS
    assert signal.SIGTERM in STOP_SIGNALS
    if hasattr(signal, "SIGHUP"):
        assert signal.Signals["SIGHUP"] in STOP_SIGNALS, "a dropped ssh session must stop the agent"


# ----------------------------------------------------------------------
# Delivery shape 3: deliver into a loop that awaits while idle
# ----------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="add_signal_handler is POSIX-only")
def test_loop_stop_on_signals_runs_the_callback_in_the_loop() -> None:
    async def scenario() -> bool:
        woken = asyncio.Event()
        with loop_stop_on_signals(StopRequest(), woken.set):
            signal.raise_signal(signal.Signals["SIGHUP"])
            async with asyncio.timeout(2):
                await woken.wait()
        return True

    assert asyncio.run(scenario())


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="add_signal_handler is POSIX-only")
def test_loop_stop_on_signals_restores_the_handler_it_replaced(sigterm_sentinel: Any) -> None:
    """asyncio's remove_signal_handler resets to SIG_DFL regardless of what it replaced.

    The companion TUI nests this inside interrupt_on_signals to cover the gap
    around Textual's loop; without the restore, that gap would come back with
    the default disposition -- i.e. a signal there killing the process before
    the park.
    """

    async def scenario() -> None:
        with loop_stop_on_signals(StopRequest(), lambda: None):
            pass

    asyncio.run(scenario())

    assert signal.getsignal(signal.SIGTERM) is sigterm_sentinel


# ----------------------------------------------------------------------
# park(): where every stop path ends
# ----------------------------------------------------------------------


def test_park_cuts_servo_torque_and_reports_both_ends_of_the_park(capsys: pytest.CaptureFixture[str]) -> None:
    driver = FakeDriver()
    robot = _robot(driver)
    robot.connect()

    park(robot)

    assert driver.disconnected
    err = capsys.readouterr().err
    assert "parking the robot" in err
    assert "park complete -- servo torque released" in err


def test_park_is_quiet_and_harmless_on_a_robot_that_was_never_connected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Entry points put this in a finally without first working out whether it is needed."""
    park(_robot(FakeDriver()))

    assert capsys.readouterr().err == ""


def test_park_a_second_time_does_not_announce_a_park_that_did_not_happen(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The TUI's finally runs after the graceful path already disconnected."""
    driver = FakeDriver()
    robot = _robot(driver)
    robot.connect()
    park(robot)
    capsys.readouterr()

    park(robot)

    assert capsys.readouterr().err == ""


def test_park_does_not_re_raise_the_interrupt_that_asked_for_it(capsys: pytest.CaptureFixture[str]) -> None:
    """disconnect() re-raises a mid-teardown interrupt; nothing above the park needs to see it."""
    driver = FakeDriver()
    robot = _robot(driver, mic=InterruptingPeripheral(KeyboardInterrupt()))
    robot.connect()

    park(robot)

    assert driver.disconnected
    # The park completed. Reporting it as a failure would tell an operator to
    # unplug a robot whose torque is already off.
    report = capsys.readouterr().err
    assert "PARK FAILED" not in report
    assert "park complete" in report


def test_park_does_not_raise_a_driver_failure_out_of_a_callers_finally(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every entry point calls park() from a finally, so a raise there replaces the real outcome."""

    class _FailingDriver(FakeDriver):
        def disconnect(self) -> None:
            raise RuntimeError("bus went away")

    robot = _robot(_FailingDriver())
    robot.connect()

    park(robot)  # must not raise


def test_park_says_torque_may_still_be_on_when_the_teardown_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A held failure must not read as a completed park -- that is the one fact an operator needs."""

    class _FailingDriver(FakeDriver):
        def disconnect(self) -> None:
            raise RuntimeError("bus went away")

    robot = _robot(_FailingDriver())
    robot.connect()

    park(robot)

    err = capsys.readouterr().err
    assert "PARK FAILED" in err
    assert "torque may still be on" in err
    assert "park complete" not in err, "a failed park must never claim success"


def test_signals_ignored_restores_what_it_installed_when_a_later_install_is_refused(
    sigterm_sentinel: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial install that lost its record would ignore SIGTERM for the life of the process.

    That is the inverted outcome: the signals meant to stop the robot would
    stop it never. Install therefore happens inside a try, and the ones already
    switched are put back before giving up.
    """
    real = signal.signal
    switched: list[int] = []

    def refuse_after_the_first(signum: int, handler: Any) -> Any:
        if handler is signal.SIG_IGN:
            if switched:
                raise ValueError("interpreter refuses handler installation")
            switched.append(signum)
        return real(signum, handler)

    monkeypatch.setattr(signal, "signal", refuse_after_the_first)
    with signals_ignored(*STOP_SIGNALS):
        pass
    monkeypatch.undo()

    assert signal.getsignal(signal.SIGTERM) is sigterm_sentinel, (
        "a signal switched to SIG_IGN before the refusal stayed ignored"
    )


def test_signals_ignored_survives_a_previous_handler_that_came_from_outside_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """signal.signal returns None for a C-level disposition and refuses None back.

    park() runs this, and park() is documented as not raising -- so a TypeError
    out of the restore would come from the one call whose whole job is to
    guarantee the torque-off, inside a caller's finally.
    """
    real = signal.signal

    def report_no_previous_handler(signum: int, handler: Any) -> Any:
        real(signum, handler)
        return None

    monkeypatch.setattr(signal, "signal", report_no_previous_handler)
    with signals_ignored(signal.SIGTERM):
        pass  # must not raise on the way out


def test_interrupt_on_signals_survives_a_previous_handler_from_outside_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its finally wraps the park on the wake-word agent, so a raise there escapes park()."""
    real = signal.signal

    def report_no_previous_handler(signum: int, handler: Any) -> Any:
        real(signum, handler)
        return None

    monkeypatch.setattr(signal, "signal", report_no_previous_handler)
    with interrupt_on_signals(StopRequest(), signal.SIGTERM):
        pass  # must not raise on the way out


def test_park_holds_a_base_exception_the_teardown_re_raised(capsys: pytest.CaptureFixture[str]) -> None:
    """disconnect() re-raises a held BaseException after the torque-off; park() absorbs it.

    Anything else would leave park() from inside a caller's finally, which the
    docstring says cannot happen.
    """

    class _SystemExitingDriver(FakeDriver):
        def disconnect(self) -> None:
            raise SystemExit(3)

    robot = _robot(_SystemExitingDriver())
    robot.connect()

    park(robot)  # must not raise

    err = capsys.readouterr().err
    assert "PARK FAILED" in err
    assert "park complete" not in err


def test_park_async_releases_torque_without_blocking_the_loop() -> None:
    driver = FakeDriver()
    robot = _robot(driver)
    robot.connect()

    asyncio.run(park_async(robot))

    assert driver.disconnected


# ----------------------------------------------------------------------
# Palmimo.disconnect(): an interrupt mid-teardown must not skip the torque-off
# ----------------------------------------------------------------------


def test_disconnect_reaches_the_driver_disconnect_when_a_peripheral_close_is_interrupted() -> None:
    """The #39 inversion: peripherals half-closed, servos still energised.

    KeyboardInterrupt is not an Exception, so the per-step suppressions used to
    let it escape before the driver disconnect -- the one call that cuts torque.
    """
    driver = FakeDriver()
    mic = InterruptingPeripheral(KeyboardInterrupt())
    camera = InterruptingPeripheral(RuntimeError("camera close failed"))
    robot = _robot(driver, mic=mic, camera=camera)
    robot.connect()

    with pytest.raises(KeyboardInterrupt):
        robot.disconnect()

    assert driver.disconnected, "an interrupt mid-teardown left the servos energised"
    assert camera.closed, "the teardown stopped at the interrupted step instead of carrying on"


def test_disconnect_re_raises_the_first_interrupt_after_the_torque_off() -> None:
    """Mirrors connect()'s BaseException rollback: continue, then re-raise."""
    driver = FakeDriver()
    first = KeyboardInterrupt("first")
    robot = _robot(
        driver, mic=InterruptingPeripheral(first), camera=InterruptingPeripheral(KeyboardInterrupt("second"))
    )
    robot.connect()

    with pytest.raises(KeyboardInterrupt) as raised:
        robot.disconnect()

    assert raised.value is first
    assert driver.disconnected


def test_disconnect_still_swallows_an_ordinary_peripheral_failure() -> None:
    """A flaky peripheral must not become an error the caller has to handle."""
    driver = FakeDriver()
    robot = _robot(driver, mic=InterruptingPeripheral(RuntimeError("mic close failed")))
    robot.connect()

    robot.disconnect()

    assert driver.disconnected


# ----------------------------------------------------------------------
# Regressions found by review: the ways a teardown still reached SIG_DFL
# ----------------------------------------------------------------------


def test_stop_flag_on_signals_counts_the_second_signal_itself_not_the_shared_request(
    sigterm_sentinel: Any,
) -> None:
    """A StopRequest carried in from an earlier phase must not make the FIRST signal terminate.

    The hand-back branch restores the default disposition and re-delivers, which
    for SIGTERM means the process dies without unwinding. Latching it on the
    shared request meant an entry point that recorded a stop during startup
    would take that branch on the operator's very first signal in the main loop,
    with the park never running.
    """
    stop = StopRequest()
    stop.request()  # an earlier phase already saw one
    reached_default: list[int] = []

    def sentinel(signum: int, frame: Any) -> None:
        reached_default.append(signum)

    signal.signal(signal.SIGTERM, sentinel)
    with stop_flag_on_signals(stop) as should_stop:
        signal.raise_signal(signal.SIGTERM)

        assert should_stop() is True
        assert reached_default == [], "the first signal was treated as the second and would have killed us"


def test_stop_flag_on_signals_flag_is_readable_the_instant_the_call_returns() -> None:
    """The startup checkpoints depend on this, and an in-loop handler cannot promise it.

    A worker thread finishing appends the awaiting task's wake-up straight to
    the ready queue, while a signal only becomes a loop callback once the loop
    reads its self-pipe -- so the in-loop shape can still read false here. This
    shape sets the flag from the C-level handler, so there is nothing to drain.
    """

    async def scenario() -> bool:
        stop = StopRequest()
        with stop_flag_on_signals(stop) as should_stop:

            def work() -> None:
                signal.raise_signal(signal.SIGTERM)

            await asyncio.to_thread(work)
            return should_stop()

    previous = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, lambda *_: None)
        assert asyncio.run(scenario()) is True
    finally:
        signal.signal(signal.SIGTERM, previous)


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="add_signal_handler is POSIX-only")
def test_loop_stop_on_signals_leaves_terminating_signals_at_the_default_on_the_way_out() -> None:
    """Pinning the property every caller has to work around, so nobody assumes otherwise.

    asyncio's remove_signal_handler resets to SIG_DFL, and this restores only
    what it actually replaced. A teardown that runs after this block therefore
    has SIGTERM/SIGHUP terminating without unwinding, and must hold
    signals_ignored over itself to reach a park.
    """
    previous = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

        async def scenario() -> None:
            with loop_stop_on_signals(StopRequest(), lambda: None):
                pass

        asyncio.run(scenario())

        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_park_async_finishes_the_park_even_when_its_caller_is_cancelled() -> None:
    """asyncio.run's teardown cancels pending tasks; the park must not return half-done.

    The parking thread cannot be cancelled, so returning at the cancellation
    would drop the signal protection and report the stop as complete while the
    neck ramp was still mid-flight.
    """
    driver = FakeDriver()
    robot = _robot(driver)
    robot.connect()

    async def scenario() -> None:
        task = asyncio.ensure_future(park_async(robot))
        await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert driver.disconnected, "the park was abandoned when its caller was cancelled"


def test_park_async_does_not_return_while_the_park_thread_is_still_running() -> None:
    """The cancellation that matters is asyncio.run's, and it lands on the park, not the caller.

    `asyncio.run` cancels every task in `all_tasks()` when its main coroutine
    returns. Awaiting a Task -- what `asyncio.to_thread` gives back -- puts the
    park itself in that set, so it is cancelled directly, and `shield` does not
    stop it: shield protects the awaiting coroutine, not the awaited task. The
    wait then ends with the worker thread still in the neck ramp, `signals_ignored`
    is lifted, and a signal in that window kills the process with torque on.

    Judged at the moment `park_async` finishes rather than after `asyncio.run`
    returns: `asyncio.run` joins the executor thread on its way out either way,
    so the disconnect has happened by then whether or not the wait was honoured.
    """
    driver = _SlowDriver()
    robot = _robot(driver)
    robot.connect()
    disconnected_when_the_wait_ended: list[bool] = []
    # Held only to keep the task alive; it is deliberately never awaited, so it
    # is still pending when the loop's teardown starts.
    pending: list[asyncio.Task[None]] = []

    async def scenario() -> None:
        task = asyncio.ensure_future(park_async(robot))
        task.add_done_callback(lambda _: disconnected_when_the_wait_ended.append(driver.disconnected))
        pending.append(task)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert not pending[0].cancelled(), "the loop's teardown cancelled the park itself"
    assert disconnected_when_the_wait_ended == [True], "park_async returned before torque was cut"


def test_with_block_keeps_the_error_it_was_raising_when_the_teardown_is_interrupted() -> None:
    """Re-raising the interrupt is right on a clean exit, and wrong on top of a real fault.

    Replacing it would silently skip a caller's `except SomeError:` recovery
    path, leaving the fault they were handling only as __context__. Torque is
    off either way -- disconnect() re-raises only after the driver disconnect.
    """
    driver = FakeDriver()
    robot = _robot(driver, mic=InterruptingPeripheral(KeyboardInterrupt()))

    with pytest.raises(RuntimeError, match="servo bus fault"), robot:
        raise RuntimeError("servo bus fault")

    assert driver.disconnected, "the torque-off did not run on the exception path"


def test_with_block_reports_an_interrupt_that_landed_in_a_clean_teardown() -> None:
    """A stop that says nothing trains people to press Ctrl+C again."""
    driver = FakeDriver()
    robot = _robot(driver, mic=InterruptingPeripheral(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt), robot:
        pass

    assert driver.disconnected
