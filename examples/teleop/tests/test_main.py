"""Behavior of `palmimo_teleop.main._build_servo_driver`'s startup probe retry -- no real
hardware, no real sleep."""

from palmimo_teleop.main import PROBE_RETRY_ATTEMPTS, _build_servo_driver


def test_build_servo_driver_attaches_after_transient_probe_failures() -> None:
    # Without this, a probe that only fails on its first attempt(s) -- e.g.
    # a servo bus still settling right after a prior process was killed --
    # would fall back to compute-only even though the bus is actually fine,
    # exactly the accident that motivated retrying at all.
    attempts: list[str | None] = []

    def probe(port: str | None) -> Exception | None:
        attempts.append(port)
        return RuntimeError("bus busy") if len(attempts) < PROBE_RETRY_ATTEMPTS else None

    driver = _build_servo_driver("/dev/ttyACM0", probe=probe, sleep=lambda _seconds: None)

    assert driver is not None
    assert len(attempts) == PROBE_RETRY_ATTEMPTS


def test_build_servo_driver_degrades_to_compute_only_after_every_attempt_fails() -> None:
    # Without this, a genuinely absent bus could retry forever (or stop
    # retrying but still attach a driver), instead of cleanly degrading to
    # compute-only the way --dry-run does.
    attempts = 0

    def probe(port: str | None) -> Exception | None:
        nonlocal attempts
        attempts += 1
        return RuntimeError("no bus")

    driver = _build_servo_driver(None, probe=probe, sleep=lambda _seconds: None)

    assert driver is None
    assert attempts == PROBE_RETRY_ATTEMPTS
