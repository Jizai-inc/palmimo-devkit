import logging
from pathlib import Path

import pytest
from tests.test_robot_flight_recorder import Clock, FlightDriver

from palmimo_sdk._flight_ride import FlightRide
from palmimo_sdk._leg_safety import LegSafety
from palmimo_sdk.engine import Motion
from palmimo_sdk.flight_recorder import ServoFlightRecorder
from palmimo_sdk.io.base import ServoTelemetry
from palmimo_sdk.thermal import NeckThermalGuard


@pytest.mark.parametrize("neck", [False, True])
@pytest.mark.parametrize("clear", ["consume", "reset", "disconnect"])
def test_guard_sweep_is_consumed_and_cleared(tmp_path: Path, neck: bool, clear: str) -> None:
    clock = Clock()
    driver = FlightDriver(clock, tmp_path)
    guard = (
        NeckThermalGuard(now=clock)
        if neck
        else LegSafety(
            arm_overload_current=1000,
            leg_overload_current=1000,
            rail_undervoltage_v=6.5,
            clock=clock,
            logger=logging.getLogger(__name__),
        )
    )

    def poll(connected: bool) -> None:
        if isinstance(guard, NeckThermalGuard):
            guard.poll(driver if connected else None)
        else:
            guard.poll(driver if connected else None, Motion.IDLE, ())

    poll(True)
    if clear == "reset":
        guard.reset()
    elif clear == "disconnect":
        poll(False)
    else:
        sweep = guard.take_sweep()
        assert sweep is not None
        assert sweep[0] == 0
        assert sweep[1].position
    assert guard.take_sweep() is None


def test_ride_drops_stale_leg_context_and_consumes_neck_once(tmp_path: Path) -> None:
    import json

    clock = Clock()
    driver = FlightDriver(clock, tmp_path)
    recorder = ServoFlightRecorder(tmp_path, clock=clock)
    ride = FlightRide(recorder, clock=clock, logger=logging.getLogger(__name__), frame_s=1 / 60)
    ride.on_connect(driver)
    leg = ServoTelemetry(current={"leg_1_yaw": 50}, position={"leg_1_yaw": 1900})
    neck = ServoTelemetry(temperature={"neck_yaw": 30}, position={"neck_yaw": 1950})
    ride.poll(driver, Motion.IDLE, (0, leg), (0, neck), {"leg_1_yaw": 2000})
    driver.bits["leg_1_yaw"] = 32
    clock.now = 1
    ride.poll(driver, Motion.IDLE, None, None, {})
    clock.now = 2.1
    ride.poll(driver, Motion.IDLE, None, None, {})
    path = recorder.flush()
    assert path is not None
    contexts = [json.loads(line) for line in path.read_text().splitlines()[1:] if json.loads(line)["tier"] == "context"]
    assert contexts[0]["axes"]["leg_1_yaw"]["goal"] == 2000
    assert contexts[0]["axes"]["leg_1_yaw"]["temperature"] is None
    assert contexts[0]["t_neck"] == -1
    assert contexts[1]["axes"] == {}
    assert contexts[1]["t_neck"] is None
