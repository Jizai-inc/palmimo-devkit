"""NeckThermalGuard — the pure neck temperature state machine."""

from collections.abc import Sequence

import pytest

from palmimo_sdk.io.base import ServoDriver, ServoTelemetry
from palmimo_sdk.thermal import (
    NECK_COOL_C,
    NECK_HOT_C,
    NECK_MOTORS,
    NECK_STALE_S,
    NECK_WARM_C,
    NeckThermalGuard,
    NeckThermalState,
)


class FakeClock:
    """A manually-advanced clock for NeckThermalGuard(now=...)."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeTelemetryDriver(ServoDriver):
    """An in-memory driver whose read_telemetry() returns injected temperatures."""

    def __init__(self, temperatures: dict[str, int] | None = None) -> None:
        self.temperatures: dict[str, int] = temperatures or {}
        self.read_calls = 0

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
        self.read_calls += 1
        wanted = motors if motors is not None else NECK_MOTORS
        return ServoTelemetry(temperature={m: self.temperatures[m] for m in wanted if m in self.temperatures})


class RaisingDriver(FakeTelemetryDriver):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    def read_telemetry(self, motors: Sequence[str] | None = None) -> ServoTelemetry:
        self.read_calls += 1
        raise self._exc


def _all_neck(temp: int) -> dict[str, int]:
    return dict.fromkeys(NECK_MOTORS, temp)


@pytest.mark.parametrize(
    ("temp", "expected"),
    [
        (NECK_WARM_C - 1, NeckThermalState.NORMAL),
        (54, NeckThermalState.NORMAL),
        (NECK_WARM_C, NeckThermalState.WARM),
        (55, NeckThermalState.WARM),
        (61, NeckThermalState.WARM),
        (NECK_HOT_C, NeckThermalState.HOT),
        (62, NeckThermalState.HOT),
        (67, NeckThermalState.HOT),
    ],
)
def test_poll_classifies_temperature_into_state(temp: int, expected: NeckThermalState) -> None:
    """A single sweep below/at/above each threshold lands in the matching state."""
    guard = NeckThermalGuard()
    guard.poll(FakeTelemetryDriver(_all_neck(temp)))
    assert guard.state is expected
    assert guard.temperature_c == temp


def test_hot_holds_until_cooled_to_the_cool_threshold() -> None:
    """Without hysteresis, a neck oscillating near HOT would flap the lockout every poll -- that
    would thrash look()/gestures between engaged and free every second. HOT must hold through any
    reading above NECK_COOL_C, even one that has dropped out of the HOT band, and only clear at/below
    NECK_COOL_C.
    """
    clock = FakeClock()
    guard = NeckThermalGuard(now=clock)
    driver = FakeTelemetryDriver(_all_neck(NECK_HOT_C))
    guard.poll(driver)
    assert guard.state is NeckThermalState.HOT
    assert guard.neck_lock_active is True

    clock.advance(1.0)
    driver.temperatures = _all_neck(NECK_COOL_C + 1)  # below HOT, still above NECK_COOL_C
    guard.poll(driver)
    assert guard.state is NeckThermalState.HOT
    assert guard.neck_lock_active is True

    clock.advance(1.0)
    driver.temperatures = _all_neck(NECK_COOL_C)
    guard.poll(driver)
    assert guard.state is NeckThermalState.WARM  # NECK_COOL_C == NECK_WARM_C -- releases straight to WARM
    assert guard.neck_lock_active is False


def test_poll_is_throttled_to_the_poll_interval() -> None:
    """Without throttling, reading telemetry every control cycle (60/s) would burn bus time the
    frame's own position writes need; the sweep must only happen once per NECK_POLL_INTERVAL_S
    regardless of how often poll() is called.
    """
    clock = FakeClock()
    guard = NeckThermalGuard(now=clock)
    driver = FakeTelemetryDriver(_all_neck(30))
    for _ in range(60):
        guard.poll(driver)
        clock.advance(1.0 / 60)
    assert driver.read_calls == 1


def test_poll_marks_unmonitored_when_driver_lacks_read_telemetry() -> None:
    """A driver with no telemetry support must not be silently treated as healthy -- callers need
    UNMONITORED to distinguish "nothing wrong" from "nothing observed".
    """

    class NoTelemetryDriver(ServoDriver):
        @property
        def is_connected(self) -> bool:
            return True

        def connect(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

        def _write_positions(self, positions: dict[str, int]) -> None:
            pass

    guard = NeckThermalGuard()
    guard.poll(NoTelemetryDriver())
    assert guard.state is NeckThermalState.UNMONITORED
    assert guard.temperature_c is None


def test_poll_keeps_last_state_when_read_raises() -> None:
    """A read failure must not silently drop the guard's last judgement (e.g. HOT), or a robot
    stuck HOT could have its look() lockout released just because one sweep glitched.
    """
    clock = FakeClock()
    guard = NeckThermalGuard(now=clock)
    guard.poll(FakeTelemetryDriver(_all_neck(NECK_HOT_C)))
    assert guard.state is NeckThermalState.HOT

    clock.advance(1.0)
    guard.poll(RaisingDriver(RuntimeError("bus error")))
    assert guard.state is NeckThermalState.HOT
    assert guard.temperature_c == NECK_HOT_C


@pytest.mark.parametrize("missing", [{"neck_yaw"}, {"neck_pitch1", "neck_pitch2", "neck_yaw"}])
def test_poll_keeps_last_state_when_a_neck_motor_is_unread(missing: set[str]) -> None:
    """A motor absent from the sweep is unknown, not healthy -- one dropped motor (or all of them)
    must not downgrade an already-established judgement to something rosier (or UNMONITORED).
    """
    clock = FakeClock()
    guard = NeckThermalGuard(now=clock)
    guard.poll(FakeTelemetryDriver(_all_neck(NECK_WARM_C)))
    assert guard.state is NeckThermalState.WARM

    clock.advance(1.0)
    partial = {m: t for m, t in _all_neck(30).items() if m not in missing}
    guard.poll(FakeTelemetryDriver(partial))
    assert guard.state is NeckThermalState.WARM
    assert guard.temperature_c == NECK_WARM_C


def test_poll_stays_unmonitored_when_first_sweep_is_incomplete() -> None:
    """The bootstrap case: with no prior judgement, an incomplete sweep has nothing to fall back
    to, so it must report UNMONITORED rather than guessing NORMAL.
    """
    guard = NeckThermalGuard()
    guard.poll(FakeTelemetryDriver({"neck_yaw": 30}))
    assert guard.state is NeckThermalState.UNMONITORED
    assert guard.temperature_c is None


def test_poll_is_silently_unmonitored_with_no_driver(caplog: pytest.LogCaptureFixture) -> None:
    """Compute-only mode (driver=None) is expected, not a fault -- unlike the unsupported-driver
    and read-failure cases, this path must not log anything (a warning here would spam every
    compute-only Palmimo(), which is a normal, unremarkable mode).
    """
    guard = NeckThermalGuard()
    with caplog.at_level("WARNING"):
        guard.poll(None)
    assert guard.state is NeckThermalState.UNMONITORED
    assert guard.temperature_c is None
    assert caplog.records == []


def test_poll_releases_the_lock_when_the_driver_disconnects() -> None:
    """driver=None means nothing is left to write a centering command to -- the lock must release,
    unlike the stale-telemetry case where it must NOT (see test_lock_survives_going_stale below).
    """
    guard = NeckThermalGuard()
    guard.poll(FakeTelemetryDriver(_all_neck(NECK_HOT_C)))
    assert guard.neck_lock_active is True

    guard.poll(None)
    assert guard.state is NeckThermalState.UNMONITORED
    assert guard.neck_lock_active is False


def test_poll_falls_back_to_unmonitored_once_stale() -> None:
    """A motor that never comes back (a dead connector, a wedged ID) must not leave a stale
    judgement in place forever -- once NECK_STALE_S has passed with no COMPLETE sweep, the guard
    must give up and report UNMONITORED rather than keep trusting an old HOT reading.
    """
    clock = FakeClock()
    guard = NeckThermalGuard(now=clock)
    guard.poll(FakeTelemetryDriver(_all_neck(NECK_HOT_C)))
    assert guard.state is NeckThermalState.HOT
    assert guard.last_complete_read_at == clock.value

    driver = FakeTelemetryDriver({m: NECK_HOT_C for m in NECK_MOTORS if m != "neck_yaw"})  # neck_yaw never answers
    clock.advance(NECK_STALE_S - 1)
    guard.poll(driver)
    assert guard.state is NeckThermalState.HOT  # still short of NECK_STALE_S

    clock.advance(1.0)
    guard.poll(driver)
    assert guard.state is NeckThermalState.UNMONITORED
    assert guard.temperature_c is None


def test_lock_survives_going_stale_and_only_clears_on_a_cool_complete_sweep() -> None:
    """A neck that was HOT and then loses telemetry must stay locked -- reporting UNMONITORED while
    quietly releasing the lock would let a caller drive an unobserved, possibly still-hot neck. The
    lock must only clear once a COMPLETE sweep actually shows it has cooled.
    """
    clock = FakeClock()
    guard = NeckThermalGuard(now=clock)
    guard.poll(FakeTelemetryDriver(_all_neck(NECK_HOT_C)))
    assert guard.neck_lock_active is True

    driver = FakeTelemetryDriver({m: NECK_HOT_C for m in NECK_MOTORS if m != "neck_yaw"})  # neck_yaw never answers
    clock.advance(NECK_STALE_S)
    guard.poll(driver)
    assert guard.state is NeckThermalState.UNMONITORED
    assert guard.neck_lock_active is True  # still locked, even though state can no longer confirm it

    clock.advance(1.0)
    guard.poll(FakeTelemetryDriver(_all_neck(50)))  # a complete sweep, well below NECK_COOL_C
    assert guard.state is NeckThermalState.NORMAL
    assert guard.neck_lock_active is False


def test_poll_stays_current_when_complete_sweeps_keep_landing() -> None:
    """The staleness fallback must not fire just because time has passed -- only when no COMPLETE
    sweep has landed in that time. A guard fed a fresh complete reading every poll must stay put.
    """
    clock = FakeClock()
    guard = NeckThermalGuard(now=clock)
    driver = FakeTelemetryDriver(_all_neck(NECK_WARM_C))
    for _ in range(int(NECK_STALE_S) + 5):
        clock.advance(1.0)
        guard.poll(driver)
    assert guard.state is NeckThermalState.WARM


def test_unsupported_latch_is_re_evaluated_after_reset() -> None:
    """The unsupported-driver latch is meant for the CAPABILITY not changing mid-connection, not for
    a reconnect to a different (or repaired) driver -- reset() must give the next driver a fresh
    NotImplementedError check rather than carrying the old driver's verdict forward forever.
    """
    guard = NeckThermalGuard()
    guard.poll(RaisingDriver(NotImplementedError()))
    assert guard.state is NeckThermalState.UNMONITORED

    telemetry_driver = FakeTelemetryDriver(_all_neck(NECK_WARM_C))
    guard.poll(telemetry_driver)  # latched -- would stay UNMONITORED without reset()
    assert guard.state is NeckThermalState.UNMONITORED
    assert telemetry_driver.read_calls == 0

    guard.reset()
    guard.poll(telemetry_driver)
    assert guard.state is NeckThermalState.WARM
