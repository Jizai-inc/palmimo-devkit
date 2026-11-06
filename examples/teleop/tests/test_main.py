"""Behavior of `palmimo_teleop.main`'s startup hardware probes (servo bus retry, face display) --
no real hardware, no real sleep."""

from unittest.mock import MagicMock, patch

import pytest

from palmimo_teleop.main import PROBE_RETRY_ATTEMPTS, _build_face_display, _build_servo_driver


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


@pytest.mark.parametrize("error_name", ["ResourceBusyError", "ReservationSetupError"])
def test_build_servo_driver_propagates_reservation_errors(error_name: str) -> None:
    reservation = pytest.importorskip("palmimo_sdk.reservation")
    error_type = getattr(reservation, error_name)
    error = error_type("servo_bus")

    with (
        patch("palmimo_teleop.main.DynamixelDriver", return_value=MagicMock(connect=MagicMock(side_effect=error))),
        pytest.raises(error_type),
    ):
        _build_servo_driver("fake", sleep=lambda _seconds: None)


def test_build_face_display_attaches_when_found_and_it_opens() -> None:
    probed: list[str] = []

    def probe(port: str) -> Exception | None:
        probed.append(port)
        return None

    display = _build_face_display(find_port=lambda: "/dev/ttyACM1", probe=probe)

    assert display is not None
    assert probed == ["/dev/ttyACM1"]


def test_build_face_display_runs_without_a_face_when_none_is_found() -> None:
    def probe(port: str) -> Exception | None:
        raise AssertionError("must not probe when no face display is attached")

    assert _build_face_display(find_port=lambda: None, probe=probe) is None


def test_build_face_display_runs_without_a_face_when_it_fails_to_open() -> None:
    # Without this, a face that is found but cannot open would be attached to
    # `Palmimo`, whose `connect()` then fails and rolls back the servo bus too.
    display = _build_face_display(find_port=lambda: "/dev/ttyACM1", probe=lambda _port: RuntimeError("busy"))

    assert display is None


@pytest.mark.parametrize("error_name", ["ResourceBusyError", "ReservationSetupError"])
def test_build_face_display_propagates_reservation_errors(error_name: str) -> None:
    reservation = pytest.importorskip("palmimo_sdk.reservation")
    error_type = getattr(reservation, error_name)
    error = error_type("display")

    with (
        patch("palmimo_teleop.main.FaceDisplay", return_value=MagicMock(connect=MagicMock(side_effect=error))),
        pytest.raises(error_type),
    ):
        _build_face_display(find_port=lambda: "fake")
