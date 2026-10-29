"""TelemetryPoller — setting aside motors that stay silent so they cannot hide the ones behind them."""

import logging
from collections.abc import Sequence

import pytest

from palmimo_sdk.io.base import ServoDriver, ServoTelemetry
from palmimo_sdk.telemetry import (
    EMPTY_READS_TO_BLIND,
    RECHECK_INTERVAL_S,
    SILENT_SWEEPS_TO_SET_ASIDE,
    TelemetryPoller,
)


BUS = ("m1", "m2", "m3", "m4")
logger = logging.getLogger("test_telemetry")


class BusDriver(ServoDriver):
    """Sweeps in bus order and stops at the first silent motor, as the real bus does."""

    def __init__(self) -> None:
        self.silent: set[str] = set()
        self.requests: list[tuple[str, ...]] = []

    @property
    def is_connected(self) -> bool:
        return True

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def _write_positions(self, positions: dict[str, int]) -> None:
        pass

    def read_telemetry(self, motors: Sequence[str] | None = None) -> ServoTelemetry:
        asked = tuple(m for m in BUS if motors is None or m in motors)
        self.requests.append(asked)
        current: dict[str, int] = {}
        for i, motor in enumerate(asked):
            if motor in self.silent:
                return ServoTelemetry(current=current, silent=(motor,), unreached=asked[i + 1 :])
            current[motor] = 1
        return ServoTelemetry(current=current)


def _poller() -> TelemetryPoller:
    return TelemetryPoller(BUS, 0.1, guard_name="test guard", logger=logger)


def _read(poller: TelemetryPoller, driver: BusDriver, now: float) -> ServoTelemetry:
    telemetry = poller.read(driver, now, "read failed")
    assert telemetry is not None
    return telemetry


def _set_m2_aside(poller: TelemetryPoller, driver: BusDriver) -> float:
    driver.silent = {"m2"}
    now = 0.0
    for _ in range(SILENT_SWEEPS_TO_SET_ASIDE):
        _read(poller, driver, now)
        now += 0.1
    return now


def test_silent_motor_is_set_aside_on_the_third_sweep_not_the_second() -> None:
    poller, driver = _poller(), BusDriver()
    driver.silent = {"m2"}
    _read(poller, driver, 0.0)
    _read(poller, driver, 0.1)
    assert poller.unwatched == ()

    _read(poller, driver, 0.2)

    assert poller.unwatched == ("m2",)
    assert [(c.motor, c.watched) for c in poller.watch_changes.drain()] == [("m2", False)]


def test_motors_behind_a_set_aside_motor_are_read_again() -> None:
    poller, driver = _poller(), BusDriver()
    now = _set_m2_aside(poller, driver)

    sweep = _read(poller, driver, now)

    assert set(sweep.current) == {"m1", "m3", "m4"}
    assert driver.requests[-1] == ("m1", "m3", "m4")


def test_motors_behind_a_silent_one_are_not_set_aside_with_it() -> None:
    poller, driver = _poller(), BusDriver()
    driver.silent = {"m1"}
    now = 0.0
    for _ in range(SILENT_SWEEPS_TO_SET_ASIDE * 2):
        _read(poller, driver, now)
        now += 0.1

    assert poller.unwatched == ("m1",)
    assert [c.motor for c in poller.watch_changes.drain()] == ["m1"]


def test_unreached_streak_alone_never_sets_a_motor_aside() -> None:
    poller = _poller()

    class AllUnreached(BusDriver):
        def read_telemetry(self, motors: Sequence[str] | None = None) -> ServoTelemetry:
            return ServoTelemetry(unreached=tuple(motors or BUS))

    driver = AllUnreached()
    for i in range(SILENT_SWEEPS_TO_SET_ASIDE * 3):
        _read(poller, driver, i * 0.1)

    assert poller.unwatched == ()


def test_set_aside_motor_is_rechecked_alone_after_the_interval_and_restored_when_it_answers() -> None:
    poller, driver = _poller(), BusDriver()
    now = _set_m2_aside(poller, driver)
    poller.watch_changes.drain()

    _read(poller, driver, now)  # before the interval: m2 is not asked
    assert "m2" not in driver.requests[-1]

    driver.silent = set()
    now += RECHECK_INTERVAL_S
    sweep = _read(poller, driver, now)

    assert "m2" in driver.requests[-1]
    assert "m2" in sweep.current
    assert poller.unwatched == ()
    assert poller.restored == ("m2",)
    assert [(c.motor, c.watched) for c in poller.watch_changes.drain()] == [("m2", True)]

    _read(poller, driver, now + 0.1)
    assert poller.restored == ()


def test_set_aside_motor_that_stays_silent_is_rechecked_one_motor_at_a_time() -> None:
    poller, driver = _poller(), BusDriver()
    driver.silent = {"m2", "m4"}
    now = 0.0
    for _ in range(SILENT_SWEEPS_TO_SET_ASIDE):
        _read(poller, driver, now)
        now += 0.1
    # m2 cuts the sweep off, so m4 needs its own silent sweeps once m2 is aside.
    for _ in range(SILENT_SWEEPS_TO_SET_ASIDE):
        _read(poller, driver, now)
        now += 0.1
    assert poller.unwatched == ("m2", "m4")

    now += RECHECK_INTERVAL_S
    sweep = _read(poller, driver, now)
    assert driver.requests[-1] == ("m1", "m2", "m3")
    assert set(sweep.current) == {"m1"}
    assert poller.unwatched == ("m2", "m4")

    now += RECHECK_INTERVAL_S
    _read(poller, driver, now)
    assert driver.requests[-1] == ("m1", "m3", "m4")


def test_read_returns_no_sweep_while_every_motor_is_set_aside_and_no_recheck_is_due() -> None:
    poller, driver = TelemetryPoller(("m1",), 0.1, guard_name="test guard", logger=logger), BusDriver()
    driver.silent = {"m1"}
    now = 0.0
    for _ in range(SILENT_SWEEPS_TO_SET_ASIDE):
        _read(poller, driver, now)
        now += 0.1
    asked = len(driver.requests)

    assert poller.read(driver, now, "read failed") is None
    assert len(driver.requests) == asked


def test_one_silent_motor_at_the_head_of_the_bus_is_not_reported_as_a_blind_guard(
    caplog: pytest.LogCaptureFixture,
) -> None:
    poller, driver = _poller(), BusDriver()
    driver.silent = {"m1"}
    now = 0.0
    with caplog.at_level(logging.ERROR, logger=logger.name):
        for _ in range(SILENT_SWEEPS_TO_SET_ASIDE + 2):
            poller.read(driver, now, "read failed")
            now += 0.1

    assert poller.unwatched == ("m1",)
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_blind_error_is_logged_once_and_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    poller, driver = _poller(), BusDriver()
    driver.silent = {"m1", "m2", "m3"}
    now = 0.0
    with caplog.at_level(logging.ERROR, logger=logger.name):
        for _ in range(SILENT_SWEEPS_TO_SET_ASIDE * 3 + 6):
            poller.read(driver, now, "read failed")
            now += 0.1

    assert len(poller.unwatched) == 3
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


class _NoAnswerDriver(BusDriver):
    """Every sweep yields no motor, the way *failure* says, until ``answering`` is set."""

    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure
        self.answering = False

    def read_telemetry(self, motors: Sequence[str] | None = None) -> ServoTelemetry:
        if self.answering:
            return super().read_telemetry(motors)
        if self.failure == "raises":
            raise OSError("port read failed")
        return ServoTelemetry(unreached=tuple(motors or BUS))


@pytest.mark.parametrize("failure", ["unreached", "raises"])
def test_blind_error_is_logged_once_when_no_read_carries_a_motor(
    failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    poller, driver = _poller(), _NoAnswerDriver(failure)

    def errors() -> int:
        return len([r for r in caplog.records if r.levelno == logging.ERROR])

    with caplog.at_level(logging.WARNING, logger=logger.name):
        for i in range(EMPTY_READS_TO_BLIND - 1):
            poller.read(driver, i * 0.1, "read failed")
        assert errors() == 0
        for i in range(EMPTY_READS_TO_BLIND - 1, EMPTY_READS_TO_BLIND * 3):
            poller.read(driver, i * 0.1, "read failed")
        assert errors() == 1

        driver.answering = True
        caplog.clear()
        poller.read(driver, 10.0, "read failed")

    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "watch the bus" in caplog.records[0].getMessage()


def test_a_flapping_motor_is_logged_once_per_interval_but_every_change_is_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    poller, driver = _poller(), BusDriver()
    with caplog.at_level(logging.WARNING, logger=logger.name):
        now = _set_m2_aside(poller, driver) + RECHECK_INTERVAL_S
        driver.silent = set()
        _read(poller, driver, now)
        driver.silent = {"m2"}
        for _ in range(SILENT_SWEEPS_TO_SET_ASIDE):
            now += 0.1
            _read(poller, driver, now)

    assert [c.watched for c in poller.watch_changes.drain()] == [False, True, False]
    assert len([r for r in caplog.records if "m2" in r.getMessage()]) == 1
