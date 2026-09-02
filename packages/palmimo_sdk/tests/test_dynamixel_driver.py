"""DynamixelDriver — contract exercised against a fake bus (no HW).

These tests pin the driver's framing logic: connect lifecycle, neutral backfill,
int casting, and torque-on-disconnect behavior. A fake bus is injected via
``bus_factory`` so none of it touches hardware.
"""

import signal
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

import palmimo_sdk.robot as robot_module
from palmimo_sdk import (
    DynamixelConnectTimeoutError,
    DynamixelDriver,
    Palmimo,
    PortDetectionError,
    find_servo_port,
)
from palmimo_sdk.io._dynamixel_bus import SpanRead
from palmimo_sdk.kinematics import NEUTRAL


MOTOR_NAMES = [f"leg_{leg}_{joint}" for leg in range(1, 7) for joint in ("yaw", "pitch1", "pitch2")] + [
    "neck_pitch1",
    "neck_pitch2",
    "neck_yaw",
]


class _RobotTimeStub:
    """Stand-in for robot.py's own view of the stdlib ``time`` module.

    Patched onto ``palmimo_sdk.robot``'s ``time`` attribute (instead of
    ``time.sleep`` directly) so a test's sleep override is confined to
    robot.py's use of it: the real module — and anything else in the process
    sleeping concurrently — is untouched. ``sleep`` runs *on_sleep*; every
    other attribute delegates to the real module.
    """

    def __init__(self, on_sleep: Callable[..., None]) -> None:
        self._on_sleep = on_sleep

    def sleep(self, *args: Any) -> None:
        self._on_sleep(*args)

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


class FakeBus:
    """Minimal stand-in for :class:`palmimo_sdk.io._dynamixel_bus.DynamixelBus`.

    Records ``sync_write`` calls and tracks connection/torque state so the
    driver's behavior can be asserted without touching hardware.
    """

    def __init__(
        self,
        present: dict[str, int | None] | None = None,
        read_none: bool = False,
        fail_write_address: str | None = None,
    ) -> None:
        self.motors: dict[str, None] = dict.fromkeys(MOTOR_NAMES)
        self.is_connected = True
        # sync_write appends an (address, data) 2-tuple; write appends an
        # (address, motor, value) 3-tuple — a variable-length list that allows both.
        self.writes: list[tuple[Any, ...]] = []
        self.disconnect_calls: list[bool] = []
        # One ordered log of every bus call. `writes` and `disconnect_calls` are
        # separate lists, so a test reading them cannot tell which happened
        # first — this log exists for the contracts that are about sequence
        # (the neck soft-release has to land BEFORE torque is cut).
        self.events: list[tuple[Any, ...]] = []
        self.present = present if present is not None else dict.fromkeys(MOTOR_NAMES, NEUTRAL)
        self.read_none = read_none  # sync_read returns None (batch read failure)
        self.fail_write_address = fail_write_address  # raise on sync_write to this address
        self.read_calls = 0
        # Telemetry the span read hands back: motor -> register -> raw value.
        # A motor left out of it is one that did not answer.
        self.telemetry: dict[str, dict[str, int]] = {
            name: {"Present_Current": 100, "Present_Input_Voltage": 47, "Present_Temperature": 30}
            for name in MOTOR_NAMES
        }
        self.span_reads: list[tuple[tuple[str, ...], tuple[str, ...] | None]] = []

    def sync_write(self, address: str, data: Any, normalize: bool = False) -> None:
        if address == self.fail_write_address:
            raise RuntimeError(f"simulated bus failure on {address}")
        self.writes.append((address, data))
        self.events.append(("sync_write", address))

    def write(self, address: str, motor: str, value: Any, normalize: bool = False) -> None:
        # Per-motor write (e.g. the neck soft-release). Recorded as a 3-tuple so
        # tests can tell it apart from a broadcast sync_write 2-tuple.
        if address == self.fail_write_address:
            raise RuntimeError(f"simulated bus failure on {address}")
        self.writes.append((address, motor, value))
        self.events.append(("write", address, motor))

    def sync_read(self, address: str, normalize: bool = False, num_retry: int = 0) -> dict[str, int | None] | None:
        self.read_calls += 1
        if self.read_none:
            return None
        return dict(self.present)

    def sync_read_span(
        self,
        fields: Any,
        *,
        motors: Any = None,
        num_retry: int = 0,
    ) -> SpanRead:
        names = list(MOTOR_NAMES) if motors is None else [n for n in MOTOR_NAMES if n in set(motors)]
        self.span_reads.append((tuple(fields), None if motors is None else tuple(motors)))
        values: dict[str, dict[str, int]] = {}
        silent: list[str] = []
        unreached: list[str] = []
        for name in names:
            if silent:
                unreached.append(name)
            elif name in self.telemetry:
                values[name] = dict(self.telemetry[name])
            else:
                silent.append(name)
        return SpanRead(values, tuple(silent), tuple(unreached))

    def disconnect(self, disable_torque: bool) -> None:
        self.disconnect_calls.append(disable_torque)
        self.events.append(("disconnect", disable_torque))
        self.is_connected = False


def make_driver(
    present: dict[str, int | None] | None = None,
    read_none: bool = False,
    fail_write_address: str | None = None,
    **kwargs: Any,
) -> tuple[DynamixelDriver, FakeBus, list[tuple[Any, ...]]]:
    """Build a driver wired to a fresh FakeBus, returning both."""
    bus = FakeBus(present=present, read_none=read_none, fail_write_address=fail_write_address)
    calls: list[tuple[Any, ...]] = []

    def factory(port: str, motor_model: str, baudrate: int, profile_velocity: int, calibration: Any = None) -> FakeBus:
        calls.append((port, motor_model, baudrate, profile_velocity, calibration))
        return bus

    driver = DynamixelDriver("/dev/fake", bus_factory=factory, **kwargs)
    return driver, bus, calls


def test_connect_opens_bus_with_configured_params() -> None:
    """connect() passes the configured values to the bus factory and sets is_connected to True."""
    driver, _bus, calls = make_driver(baudrate=57600, motor_model="xl330-m288", profile_velocity=120)
    assert driver.is_connected is False
    driver.connect()
    assert driver.is_connected is True
    assert calls == [("/dev/fake", "xl330-m288", 57600, 120, None)]


def test_connect_defaults_to_uncalibrated_bus() -> None:
    """Without an explicit calibration, the bus opens raw/uncalibrated (None)."""
    driver, _bus, calls = make_driver()
    driver.connect()
    assert calls[0][4] is None


def test_connect_forwards_calibration_to_bus() -> None:
    """A calibration argument is forwarded to the bus factory unchanged."""
    calib = {"neck_pitch1": object()}
    driver, _bus, calls = make_driver(calibration=calib)
    driver.connect()
    assert calls[0][4] is calib


def test_connect_is_idempotent() -> None:
    """connect() is a no-op (doesn't recreate the bus) once already connected."""
    driver, _bus, calls = make_driver()
    driver.connect()
    driver.connect()
    assert len(calls) == 1


def test_write_positions_fills_missing_with_neutral() -> None:
    """Missing motors are backfilled with NEUTRAL; all 21 motors are sent in Goal_Position."""
    driver, bus, _ = make_driver()
    driver.connect()
    driver.write_positions({"leg_1_yaw": 1000})
    address, goal = bus.writes[-1]
    assert address == "Goal_Position"
    assert set(goal) == set(MOTOR_NAMES)
    assert goal["leg_1_yaw"] == 1000
    assert all(goal[name] == NEUTRAL for name in MOTOR_NAMES if name != "leg_1_yaw")


def test_write_positions_treats_none_as_neutral() -> None:
    """A key with a None value is backfilled with NEUTRAL like a missing key, without int(None) crashing."""
    driver, bus, _ = make_driver()
    driver.connect()
    driver.write_positions({"leg_1_yaw": 1000, "neck_yaw": None})  # type: ignore[dict-item]  # deliberately beyond the declared contract -- verifying the defensive None backfill
    _address, goal = bus.writes[-1]
    assert goal["leg_1_yaw"] == 1000
    assert goal["neck_yaw"] == NEUTRAL


def test_write_positions_casts_to_int() -> None:
    """Float values are cast to int before being sent."""
    driver, bus, _ = make_driver()
    driver.connect()
    driver.write_positions({"neck_yaw": 2047.9})  # type: ignore[dict-item]  # deliberately beyond the declared contract -- verifying the int() cast
    _address, goal = bus.writes[-1]
    assert goal["neck_yaw"] == 2047
    assert isinstance(goal["neck_yaw"], int)


def test_write_positions_before_connect_raises() -> None:
    """write_positions before connecting raises RuntimeError."""
    driver, _bus, _ = make_driver()
    with pytest.raises(RuntimeError, match="not connected"):
        driver.write_positions({"leg_1_yaw": 2048})


def test_disconnect_cuts_torque_by_default() -> None:
    """The default disconnect cuts torque (disable_torque=True) and clears the connected state."""
    driver, bus, _ = make_driver()
    driver.connect()
    driver.disconnect()
    assert bus.disconnect_calls == [True]
    assert driver.is_connected is False


def test_keep_torque_on_disconnect() -> None:
    """keep_torque_on_disconnect=True keeps torque on (disable_torque=False)."""
    driver, bus, _ = make_driver(keep_torque_on_disconnect=True)
    driver.connect()
    driver.disconnect()
    assert bus.disconnect_calls == [False]


def test_disconnect_when_idle_is_safe() -> None:
    """disconnect() is a no-op when not connected."""
    driver, bus, _ = make_driver()
    driver.disconnect()
    assert bus.disconnect_calls == []


def test_unsupported_motor_model_rejected() -> None:
    """An unsupported motor model raises ValueError."""
    with pytest.raises(ValueError, match="Unsupported motor_model"):
        DynamixelDriver("/dev/fake", motor_model="ax-12a")


def test_facade_streams_steps_to_driver() -> None:
    """Palmimo(driver=) streams Goal_Position to the driver on every step() while connected."""
    driver, bus, _ = make_driver()
    robot = Palmimo(driver=driver, auto_wake=False)  # wake glide is not under test here
    robot.connect()
    robot.forward()
    robot.step_n(3)
    goal_writes = [w for w in bus.writes if w[0] == "Goal_Position"]
    assert len(goal_writes) == 3
    assert all(set(goal) == set(MOTOR_NAMES) for _addr, goal in goal_writes)


def test_read_positions_returns_present_as_ints() -> None:
    """read_positions returns Present_Position as a motor-name -> int mapping."""
    driver, _bus, _ = make_driver(present={**dict.fromkeys(MOTOR_NAMES, NEUTRAL), "leg_2_pitch1": 1500})
    driver.connect()
    pos = driver.read_positions()
    assert pos["leg_2_pitch1"] == 1500
    assert all(isinstance(v, int) for v in pos.values())


def test_read_positions_before_connect_raises() -> None:
    driver, _bus, _ = make_driver()
    with pytest.raises(RuntimeError, match="not connected"):
        driver.read_positions()


def test_set_profile_velocity_zero_restores_default() -> None:
    """ticks/s = 0 writes back the driver's default speed (in register units)."""
    driver, bus, _ = make_driver(profile_velocity=300)
    driver.connect()
    driver.set_profile_velocity(0)
    assert bus.writes[-1] == ("Profile_Velocity", 300)


def test_set_profile_velocity_scalar_is_positive_and_monotonic() -> None:
    """A positive ticks/s converts to a positive register value, larger for faster speeds."""
    driver, bus, _ = make_driver()
    driver.connect()
    driver.set_profile_velocity(1000)
    slow = bus.writes[-1][1]
    driver.set_profile_velocity(4000)
    fast = bus.writes[-1][1]
    assert isinstance(slow, int) and slow >= 1
    assert fast > slow


def test_set_profile_velocity_dict_per_motor() -> None:
    """A dict argument converts per motor; a motor set to 0 reverts to the default speed."""
    driver, bus, _ = make_driver(profile_velocity=300)
    driver.connect()
    driver.set_profile_velocity({"leg_1_yaw": 4000, "neck_yaw": 0})
    _addr, value = bus.writes[-1]
    assert value["neck_yaw"] == 300  # 0 -> default speed
    assert value["leg_1_yaw"] >= 1  # positive values convert independently
    assert value["leg_1_yaw"] != value["neck_yaw"]


def test_set_profile_velocity_before_connect_raises() -> None:
    driver, _bus, _ = make_driver()
    with pytest.raises(RuntimeError, match="not connected"):
        driver.set_profile_velocity(1000)


def test_timed_return_to_neutral_software_glides_to_neutral() -> None:
    """return_to_neutral(duration=) softly interpolates each joint to neutral over
    several frames (not a profile-velocity snap); the final frame is neutral."""
    present: dict[str, int | None] = {**dict.fromkeys(MOTOR_NAMES, NEUTRAL), "leg_1_yaw": NEUTRAL + 400}
    driver, bus, _ = make_driver(present=present, profile_velocity=300)
    robot = Palmimo(driver=driver, auto_wake=False)  # wake glide is not under test here
    robot.connect()
    robot.return_to_neutral(duration=0.05)  # a few frames

    goal_writes = [w for w in bus.writes if w[0] == "Goal_Position"]
    leg1 = [v["leg_1_yaw"] for _, v in goal_writes]
    assert len(goal_writes) >= 2  # interpolated, not a single snap
    assert leg1[-1] == NEUTRAL  # final frame is neutral
    assert leg1[0] < NEUTRAL + 400  # gradual, not an instant jump to neutral
    assert all(NEUTRAL <= v <= NEUTRAL + 400 for v in leg1)  # stays within range


def test_timed_return_requires_connected_driver() -> None:
    """Timed return without a driver / while disconnected raises RuntimeError."""
    with pytest.raises(RuntimeError, match="connected driver"):
        Palmimo().return_to_neutral(duration=1.0)
    driver, _bus, _ = make_driver()
    with pytest.raises(RuntimeError, match="connected driver"):
        Palmimo(driver=driver).return_to_neutral(duration=1.0)  # not connected


def test_timed_return_rejects_nonpositive_duration() -> None:
    driver, _bus, _ = make_driver()
    robot = Palmimo(driver=driver, auto_wake=False)  # wake glide is not under test here
    robot.connect()
    with pytest.raises(ValueError, match="duration must be positive"):
        robot.return_to_neutral(duration=0)


def test_read_positions_handles_none_batch() -> None:
    """read_positions safely degrades to an empty dict when sync_read returns None for the whole batch."""
    driver, _bus, _ = make_driver(read_none=True)
    driver.connect()
    assert driver.read_positions() == {}


def test_read_positions_collapses_none_motor_to_neutral() -> None:
    """A single motor's None value collapses to NEUTRAL (int(None) doesn't crash)."""
    present = {**dict.fromkeys(MOTOR_NAMES, NEUTRAL), "leg_3_yaw": None}
    driver, _bus, _ = make_driver(present=present)
    driver.connect()
    assert driver.read_positions()["leg_3_yaw"] == NEUTRAL


def test_set_profile_velocity_dict_backfills_all_motors() -> None:
    """Even a partial dict sync_writes keys for every motor (avoids a real-hardware KeyError)."""
    driver, bus, _ = make_driver()
    driver.connect()
    driver.set_profile_velocity({"leg_1_yaw": 4000})
    _addr, value = bus.writes[-1]
    assert set(value) == set(MOTOR_NAMES)  # missing keys are backfilled


def test_timed_return_surfaces_write_failure() -> None:
    """A write failure during the glide propagates instead of being swallowed."""
    driver, _bus, _ = make_driver(fail_write_address="Goal_Position", profile_velocity=300)
    robot = Palmimo(driver=driver, auto_wake=False)  # wake glide would itself hit the simulated failure
    robot.connect()
    with pytest.raises(RuntimeError, match="simulated bus failure"):
        robot.return_to_neutral(duration=0.02)


def test_write_positions_clamps_to_safe_range() -> None:
    """goal is clamped to the safe tick range (200-3900) to avoid stalling at the mechanical limits 0/4095."""
    driver, bus, _ = make_driver()
    driver.connect()
    driver.write_positions({"leg_1_yaw": 5000, "leg_2_yaw": -100, "leg_3_yaw": 2048})
    _addr, goal = bus.writes[-1]
    assert goal["leg_1_yaw"] == 3900  # clamped to the upper bound
    assert goal["leg_2_yaw"] == 200  # clamped to the lower bound
    assert goal["leg_3_yaw"] == 2048  # unchanged within range


def test_set_profile_velocity_pairs_trapezoidal_acceleration() -> None:
    """A positive glide speed pairs with Profile_Acceleration > 0 (trapezoidal profile, smooth stop at the end)."""
    driver, bus, _ = make_driver()
    driver.connect()
    driver.set_profile_velocity(2000)
    accel_writes = [w for w in bus.writes if w[0] == "Profile_Acceleration"]
    assert accel_writes and accel_writes[-1][1] >= 1
    # acceleration is written before velocity (a velocity-only observer sees Velocity last)
    assert bus.writes[-1][0] == "Profile_Velocity"


def test_set_profile_velocity_zero_restores_rectangular_profile() -> None:
    """ticks/s=0 (reverting to default) sets Profile_Acceleration=0 (rectangular, so gait stops crisply)."""
    driver, bus, _ = make_driver(profile_velocity=300)
    driver.connect()
    driver.set_profile_velocity(0)
    accel_writes = [w for w in bus.writes if w[0] == "Profile_Acceleration"]
    assert accel_writes[-1][1] == 0
    assert bus.writes[-1] == ("Profile_Velocity", 300)


def test_set_profile_velocity_dict_pairs_accel_per_motor() -> None:
    """dict argument: moving motors get a trapezoidal profile (accel>=1), motors at the
    default speed (0) get rectangular (accel 0). All motors are backfilled."""
    driver, bus, _ = make_driver()
    driver.connect()
    driver.set_profile_velocity({"leg_1_yaw": 4000, "neck_yaw": 0})
    accel = [w for w in bus.writes if w[0] == "Profile_Acceleration"][-1][1]
    assert accel["leg_1_yaw"] >= 1
    assert accel["neck_yaw"] == 0
    assert set(accel) == set(MOTOR_NAMES)


def test_exit_softreleases_neck_before_torque_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean exit ramps down the neck's Position_P_Gain (soft release) before cutting torque."""
    monkeypatch.setattr("palmimo_sdk.robot.time.sleep", lambda *_: None)
    from palmimo_sdk.robot import _NECK_RELEASE_GAINS

    driver, bus, _ = make_driver()
    with Palmimo(driver=driver, auto_wake=False):  # wake glide is not under test here
        pass
    pgain_neck = [
        w
        for w in bus.writes
        if len(w) == 3 and w[0] == "Position_P_Gain" and w[1] in ("neck_pitch1", "neck_pitch2", "neck_yaw")
    ]
    assert pgain_neck, "neck P-gain was not ramped down"
    assert pgain_neck[-1][2] == _NECK_RELEASE_GAINS[-1]  # ends at the lowest release gain (near-limp)
    assert bus.disconnect_calls == [True]

    # Torque is cut only after the neck release finishes (so the head doesn't jerk).
    # If the order were reversed the head would drop on real hardware, so this
    # checks the order itself, not merely that both events happened.
    release_at = [
        i
        for i, event in enumerate(bus.events)
        if event[0] == "write" and event[1] == "Position_P_Gain" and event[2].startswith("neck_")
    ]
    torque_off_at = next(i for i, event in enumerate(bus.events) if event[0] == "disconnect")
    assert max(release_at) < torque_off_at, "torque was cut before the neck finished releasing"


def test_neck_softrelease_ramps_full_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    """_park_neck writes the neck gain through every stage of the release ramp, in order (independent of read)."""
    monkeypatch.setattr("palmimo_sdk.robot.time.sleep", lambda *_: None)
    from palmimo_sdk.robot import _NECK_RELEASE_GAINS

    driver, bus, _ = make_driver(read_none=True)  # fine even if read fails
    robot = Palmimo(driver=driver, auto_wake=False)  # wake glide is not under test here
    robot.connect()
    robot._park_neck()
    written = [w[2] for w in bus.writes if len(w) == 3 and w[0] == "Position_P_Gain" and w[1] == "neck_pitch1"]
    assert written == list(_NECK_RELEASE_GAINS)


def test_neck_softrelease_runs_the_whole_ramp_when_ctrl_c_is_mashed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mashed Ctrl+C must neither abort the release ramp nor rush it.

    A real SIGINT is raised at every dwell. None may surface as a
    ``KeyboardInterrupt``, and -- the discriminator that a bare
    ``except KeyboardInterrupt: continue`` around the sleep would also pass --
    every dwell must run with SIGINT actually suppressed (``SIG_IGN``), not
    merely with a handler that happens to survive being called.
    """
    from palmimo_sdk.robot import _NECK_RELEASE_GAINS

    driver, bus, _ = make_driver(read_none=True)
    robot = Palmimo(driver=driver, auto_wake=False)
    robot.connect()

    handler_during_dwell: list[object] = []

    def mash_ctrl_c(*_: Any) -> None:
        handler_during_dwell.append(signal.getsignal(signal.SIGINT))
        signal.raise_signal(signal.SIGINT)

    monkeypatch.setattr(robot_module, "time", _RobotTimeStub(mash_ctrl_c))

    installed = signal.getsignal(signal.SIGINT)
    try:
        robot._park_neck()
    except KeyboardInterrupt:
        pytest.fail("a Ctrl+C escaped _park_neck")

    written = [w[2] for w in bus.writes if len(w) == 3 and w[0] == "Position_P_Gain" and w[1] == "neck_pitch1"]
    assert written == list(_NECK_RELEASE_GAINS), "the ramp lost steps to the interrupts"
    assert signal.getsignal(signal.SIGINT) is installed, "the SIGINT handler was not restored"
    assert handler_during_dwell, "the ramp never dwelled"
    assert all(h is signal.SIG_IGN for h in handler_during_dwell), (
        "a dwell ran without SIGINT suppressed -- a real mash could have landed inside it"
    )


# ----------------------------------------------------------------------
# Auto-wake / park lifecycle: connect() glides the robot awake,
# disconnect() parks it (neutral return + neck soft-release) before closing.
# ----------------------------------------------------------------------


def test_connect_auto_runs_wake_glide(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect() with the default auto_wake=True streams a wake glide ending at the neutral targets."""
    monkeypatch.setattr("palmimo_sdk.robot.time.sleep", lambda *_: None)

    driver, bus, _ = make_driver()
    robot = Palmimo(driver=driver)  # auto_wake=True (default)
    robot.connect()

    goal_writes = [w for w in bus.writes if w[0] == "Goal_Position"]
    assert len(goal_writes) > 1  # streamed a glide, not a single snap
    last_frame = goal_writes[-1][1]
    targets = robot._neutral_targets()
    assert all(abs(last_frame[name] - target) <= 1 for name, target in targets.items())


def test_connect_with_auto_wake_false_performs_no_position_writes() -> None:
    """Palmimo(driver=..., auto_wake=False).connect() connects immediately, with no position writes."""
    driver, bus, _ = make_driver()
    robot = Palmimo(driver=driver, auto_wake=False)
    robot.connect()

    assert robot.is_connected
    goal_writes = [w for w in bus.writes if w[0] == "Goal_Position"]
    assert goal_writes == []


def test_disconnect_parks_before_closing(monkeypatch: pytest.MonkeyPatch) -> None:
    """disconnect() on a connected robot streams return_to_neutral and soft-releases the neck
    before cutting torque, mirroring test_exit_softreleases_neck_before_torque_off."""
    monkeypatch.setattr("palmimo_sdk.robot.time.sleep", lambda *_: None)
    from palmimo_sdk.robot import _NECK_RELEASE_GAINS

    driver, bus, _ = make_driver()
    robot = Palmimo(driver=driver, auto_wake=False)  # wake glide is not under test here
    robot.connect()
    bus.writes.clear()
    bus.events.clear()

    robot.disconnect()

    goal_writes = [w for w in bus.writes if w[0] == "Goal_Position"]
    assert goal_writes  # return_to_neutral streamed the leg return
    neck_gains = [w[2] for w in bus.writes if len(w) == 3 and w[0] == "Position_P_Gain" and w[1] == "neck_pitch1"]
    assert neck_gains == list(_NECK_RELEASE_GAINS)  # full soft-release ramp ran

    release_at = [
        i
        for i, event in enumerate(bus.events)
        if event[0] == "write" and event[1] == "Position_P_Gain" and event[2].startswith("neck_")
    ]
    torque_off_at = next(i for i, event in enumerate(bus.events) if event[0] == "disconnect")
    assert max(release_at) < torque_off_at, "torque was cut before the neck finished releasing"
    assert bus.disconnect_calls == [True]


def test_disconnect_with_park_false_skips_leg_return_but_still_soft_releases_neck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """disconnect(park=False) skips the return-to-neutral streaming but the neck soft-release still runs."""
    monkeypatch.setattr("palmimo_sdk.robot.time.sleep", lambda *_: None)
    from palmimo_sdk.robot import _NECK_RELEASE_GAINS

    driver, bus, _ = make_driver()
    robot = Palmimo(driver=driver, auto_wake=False)  # wake glide is not under test here
    robot.connect()
    bus.writes.clear()

    robot.disconnect(park=False)

    goal_writes = [w for w in bus.writes if w[0] == "Goal_Position"]
    assert goal_writes == []  # no leg return streamed
    neck_gains = [w[2] for w in bus.writes if len(w) == 3 and w[0] == "Position_P_Gain" and w[1] == "neck_pitch1"]
    assert neck_gains == list(_NECK_RELEASE_GAINS)  # neck soft-release still ran in full


def test_disconnect_survives_sigint_landing_between_leg_return_and_neck_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Ctrl+C landing right as the leg return finishes -- before the neck release's
    own protection would otherwise begin -- must not strand the robot: disconnect()
    has to absorb it, still run the neck release ramp in full, and still cut torque.
    """
    from palmimo_sdk.robot import _NECK_RELEASE_GAINS

    driver, bus, _ = make_driver()
    robot = Palmimo(driver=driver, auto_wake=False)  # wake glide is not under test here
    robot.connect()
    monkeypatch.setattr(robot_module, "time", _RobotTimeStub(lambda *_: None))
    real_park_neck = robot._park_neck

    def park_neck_after_sigint_in_the_seam() -> None:
        signal.raise_signal(signal.SIGINT)
        real_park_neck()

    monkeypatch.setattr(robot, "_park_neck", park_neck_after_sigint_in_the_seam)

    try:
        robot.disconnect()
    except KeyboardInterrupt:
        pytest.fail("a Ctrl+C landing between the leg return and the neck release escaped disconnect()")

    neck_gains = [w[2] for w in bus.writes if len(w) == 3 and w[0] == "Position_P_Gain" and w[1] == "neck_pitch1"]
    assert neck_gains == list(_NECK_RELEASE_GAINS), "the interrupt cut the neck release ramp short"
    assert bus.disconnect_calls == [True], "the interrupt left the port open and torque on"


def test_disconnect_survives_sigint_landing_in_a_peripheral_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The torque-off is the last statement in disconnect(), so a Ctrl+C landing in
    any of the peripheral closes before it -- each guarded only against Exception,
    which KeyboardInterrupt is not -- must still not escape and skip it.
    """

    class CameraRaisingSigintOnClose:
        def close(self) -> None:
            signal.raise_signal(signal.SIGINT)

    driver, bus, _ = make_driver()
    robot = Palmimo(driver=driver, auto_wake=False)  # wake glide is not under test here
    robot.connect()
    monkeypatch.setattr(robot_module, "time", _RobotTimeStub(lambda *_: None))
    robot._camera = CameraRaisingSigintOnClose()  # type: ignore[assignment]  # only close() is reached here

    try:
        robot.disconnect()
    except KeyboardInterrupt:
        pytest.fail("a Ctrl+C landing in a peripheral close escaped disconnect()")

    assert bus.disconnect_calls == [True], "the interrupt left the port open and torque on"


def test_exit_after_exception_soft_releases_neck_without_motion(monkeypatch: pytest.MonkeyPatch) -> None:
    """On an exception inside the with-block, no position writes happen after the exception, but the
    neck soft-release still runs (unlike the old code, which skipped _park_neck on exceptions)."""
    monkeypatch.setattr("palmimo_sdk.robot.time.sleep", lambda *_: None)
    from palmimo_sdk.robot import _NECK_RELEASE_GAINS

    driver, bus, _ = make_driver()
    with pytest.raises(RuntimeError, match="boom"), Palmimo(driver=driver, auto_wake=False) as robot:
        # wake glide is not under test here
        bus.writes.clear()
        raise RuntimeError("boom")

    goal_writes = [w for w in bus.writes if w[0] == "Goal_Position"]
    assert goal_writes == []  # no motion streamed after the exception
    neck_gains = [w[2] for w in bus.writes if len(w) == 3 and w[0] == "Position_P_Gain" and w[1] == "neck_pitch1"]
    assert neck_gains == list(_NECK_RELEASE_GAINS)  # neck still soft-released
    assert not robot.is_connected


def test_connect_rolls_back_and_soft_releases_neck_when_wake_write_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A write failure during the auto-wake glide rolls the connect back: the neck is soft-released
    (Position_P_Gain ramped down to near-limp) BEFORE the bus is disconnected, and the driver ends
    disconnected. The simulated failure lands on Goal_Position, which the wake glide streams first."""
    monkeypatch.setattr("palmimo_sdk.robot.time.sleep", lambda *_: None)
    from palmimo_sdk.robot import _NECK_RELEASE_GAINS

    driver, bus, _ = make_driver(fail_write_address="Goal_Position")
    robot = Palmimo(driver=driver)  # auto_wake=True (default): wake streams Goal_Position and fails

    with pytest.raises(RuntimeError, match="simulated bus failure"):
        robot.connect()

    neck_gains = [w[2] for w in bus.writes if len(w) == 3 and w[0] == "Position_P_Gain" and w[1] == "neck_pitch1"]
    # Suffix, not full equality: wake() first pins the neck at _WAKE_FULL_GAIN before the glide starts,
    # so the tail of the gain writes is the release ramp that matters here.
    assert neck_gains[-len(_NECK_RELEASE_GAINS) :] == list(_NECK_RELEASE_GAINS)  # full soft-release ramp ran

    release_at = [
        i
        for i, event in enumerate(bus.events)
        if event[0] == "write" and event[1] == "Position_P_Gain" and event[2].startswith("neck_")
    ]
    torque_off_at = next(i for i, event in enumerate(bus.events) if event[0] == "disconnect")
    assert max(release_at) < torque_off_at, "torque was cut before the neck finished releasing"
    assert not robot.is_connected


def test_connect_rolls_back_when_wake_interrupted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Ctrl+C during the wake glide is a KeyboardInterrupt, not an Exception — connect() must still
    roll back (soft-release the neck, disconnect the driver) rather than leaking the connection, and
    must re-raise the KeyboardInterrupt rather than swallowing it."""
    monkeypatch.setattr("palmimo_sdk.robot.time.sleep", lambda *_: None)
    from palmimo_sdk.robot import _NECK_RELEASE_GAINS

    driver, bus, _ = make_driver()
    robot = Palmimo(driver=driver)  # auto_wake=True (default)

    original_write_positions = driver.write_positions
    call_count = {"n": 0}

    def flaky_write_positions(positions: dict[str, int]) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise KeyboardInterrupt
        original_write_positions(positions)

    monkeypatch.setattr(driver, "write_positions", flaky_write_positions)

    with pytest.raises(KeyboardInterrupt):
        robot.connect()

    assert not robot.is_connected
    neck_gains = [w[2] for w in bus.writes if len(w) == 3 and w[0] == "Position_P_Gain" and w[1] == "neck_pitch1"]
    # Suffix, not full equality: wake() first pins the neck at _WAKE_FULL_GAIN before the glide starts,
    # so the tail of the gain writes is the release ramp that matters here.
    assert neck_gains[-len(_NECK_RELEASE_GAINS) :] == list(_NECK_RELEASE_GAINS)  # rollback soft-released the neck


def test_set_p_gain_reset_subset_restores_only_those_motors() -> None:
    """set_position_p_gain(None, motors=[...]) resets only the given subset to default (doesn't touch every axis)."""
    driver, bus, _ = make_driver()
    driver.connect()
    bus.writes.clear()
    subset = ["neck_pitch1", "neck_yaw"]
    driver.set_position_p_gain(None, motors=subset)
    # Per-motor writes (3-tuples) limited to exactly the requested motors.
    reset = [w for w in bus.writes if len(w) == 3 and w[0] == "Position_P_Gain"]
    assert {w[1] for w in reset} == set(subset)
    assert all(w[1] in subset for w in reset)
    # No broadcast sync_write that would reset every motor.
    assert not [w for w in bus.writes if len(w) == 2 and w[0] == "Position_P_Gain"]


def test_set_profile_velocity_units_subset_writes_only_those_motors() -> None:
    """set_profile_velocity_units(v, motors=[...]) writes per-motor only for the given subset."""
    driver, bus, _ = make_driver()
    driver.connect()
    bus.writes.clear()
    arm = ["leg_3_yaw", "leg_3_pitch1", "leg_3_pitch2"]
    driver.set_profile_velocity_units(0, motors=arm)
    pv = [w for w in bus.writes if len(w) == 3 and w[0] == "Profile_Velocity"]
    assert {w[1] for w in pv} == set(arm)
    assert all(w[2] == 0 for w in pv)


def test_connect_with_port_none_auto_detects(monkeypatch: pytest.MonkeyPatch) -> None:
    """port=None makes connect() open the bus with find_servo_port()'s result."""
    monkeypatch.setattr("palmimo_sdk.io.dynamixel.find_servo_port", lambda: "/dev/detected0")
    bus = FakeBus()
    calls: list[tuple[Any, ...]] = []

    def factory(port: str, motor_model: str, baudrate: int, profile_velocity: int, calibration: Any = None) -> FakeBus:
        calls.append((port,))
        return bus

    driver = DynamixelDriver(bus_factory=factory)
    driver.connect()
    assert calls[0][0] == "/dev/detected0"


def test_connect_with_explicit_port_skips_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit port skips calling find_servo_port()."""

    def boom() -> None:
        raise AssertionError("find_servo_port must not be called")

    monkeypatch.setattr("palmimo_sdk.io.dynamixel.find_servo_port", boom)
    driver, _bus, calls = make_driver()
    driver.connect()
    assert calls[0][0] == "/dev/fake"


def test_connect_propagates_port_detection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """port=None with detection failure propagates PortDetectionError from connect() and leaves it disconnected."""

    def fail() -> None:
        raise PortDetectionError("not found")

    monkeypatch.setattr("palmimo_sdk.io.dynamixel.find_servo_port", fail)
    driver = DynamixelDriver(bus_factory=lambda *a, **kw: FakeBus())
    with pytest.raises(PortDetectionError):
        driver.connect()
    assert not driver.is_connected


# find_servo_port — servo-bus port auto-detection (moved from test_ports.py;
# the finder lives in io/dynamixel.py with the driver whose bus it locates).
# All tests monkeypatch comports() so no hardware is needed.


class _FakePort:
    """Minimal stand-in for ``serial.tools.list_ports.ListPortInfo``."""

    def __init__(
        self,
        device: str,
        manufacturer: str = "",
        product: str = "",
        description: str = "",
        vid: int | None = None,
    ) -> None:
        self.device = device
        self.manufacturer = manufacturer
        self.product = product
        self.description = description
        self.vid = vid


def _patch_ports(monkeypatch: pytest.MonkeyPatch, ports: list[_FakePort]) -> None:
    """Patch comports() for a single test."""
    import serial.tools.list_ports

    monkeypatch.setattr(serial.tools.list_ports, "comports", lambda: ports)


def test_pattern_match_ttyacm_when_no_vid(monkeypatch: pytest.MonkeyPatch) -> None:
    """No VID match -> pattern-matches /dev/ttyACM0."""
    ports = [_FakePort("/dev/ttyACM0"), _FakePort("/dev/ttyUSB0")]
    _patch_ports(monkeypatch, ports)

    assert find_servo_port() == "/dev/ttyACM0"


def test_pattern_match_usbmodem_cu_node_when_no_vid(monkeypatch: pytest.MonkeyPatch) -> None:
    """No VID match -> pattern-matches /dev/cu.usbmodem*.

    macOS's comports() only enumerates cu.* (IOCalloutDevice) per physical port
    (per pyserial's list_ports_osx.py, confirmed on real hardware); tty.* is
    never returned, so the pattern match would come up empty without matching cu.*.
    """
    ports = [
        _FakePort("/dev/cu.usbmodem1101"),
        _FakePort("/dev/cu.Bluetooth-Incoming-Port"),
    ]
    _patch_ports(monkeypatch, ports)

    assert find_servo_port() == "/dev/cu.usbmodem1101"


def test_vid_match_on_nameless_windows_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """What a real Windows device looks like: matched by VID 0x2F5D even under a generic name."""
    ports = [
        _FakePort("COM14", description="USB シリアル デバイス (COM14)", vid=0x2F5D),
        _FakePort("COM5", description="Bluetooth リンク経由の標準シリアル (COM5)"),
    ]
    _patch_ports(monkeypatch, ports)
    assert find_servo_port() == "COM14"


def test_vid_match_takes_precedence_over_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    """A VID match takes precedence over another port that also matches the pattern
    (resolved at step 1).

    Reaching step 2's pattern matching would error with two candidates (ttyACM0
    and ttyACM1), so returning the single VID-matched port is itself the proof
    that VID outranks the pattern.
    """
    ports = [
        _FakePort("/dev/ttyACM0"),  # pattern match only (no VID)
        _FakePort("/dev/ttyACM1", vid=0x2F5D),  # VID match (also matches the pattern)
    ]
    _patch_ports(monkeypatch, ports)
    assert find_servo_port() == "/dev/ttyACM1"


def test_multiple_vid_matches_refuse_to_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple ports with VID 0x2F5D raises instead of guessing (avoids writing to the wrong port)."""
    ports = [
        _FakePort("COM14", description="USB Serial Device", vid=0x2F5D),
        _FakePort("COM15", description="USB Serial Device", vid=0x2F5D),
    ]
    _patch_ports(monkeypatch, ports)
    with pytest.raises(PortDetectionError, match=r"COM14.*COM15"):
        find_servo_port()


def test_no_candidates_raises_port_detection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero candidates -> PortDetectionError."""
    _patch_ports(monkeypatch, [_FakePort("/dev/ttyUSB0"), _FakePort("/dev/cu.Bluetooth-Incoming-Port")])

    with pytest.raises(PortDetectionError, match="not found"):
        find_servo_port()


def test_empty_port_list_raises_port_detection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ports at all -> PortDetectionError."""
    _patch_ports(monkeypatch, [])

    with pytest.raises(PortDetectionError, match="not found"):
        find_servo_port()


def test_multiple_candidates_raises_with_both_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple candidates -> PortDetectionError includes both port names."""
    ports = [
        _FakePort("/dev/ttyACM0"),
        _FakePort("/dev/ttyACM1"),
    ]
    _patch_ports(monkeypatch, ports)

    with pytest.raises(PortDetectionError) as exc_info:
        find_servo_port()

    msg = str(exc_info.value)
    assert "/dev/ttyACM0" in msg
    assert "/dev/ttyACM1" in msg


def test_multiple_pattern_candidates_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple candidates in the pattern fallback -> PortDetectionError."""
    ports = [_FakePort("/dev/ttyACM0"), _FakePort("/dev/ttyACM1")]
    _patch_ports(monkeypatch, ports)

    with pytest.raises(PortDetectionError, match="Multiple"):
        find_servo_port()


def test_error_message_contains_remedy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The error message documents the --port workaround."""
    _patch_ports(monkeypatch, [])

    with pytest.raises(PortDetectionError, match="--port"):
        find_servo_port()


# ----------------------------------------------------------------------
# connect() fail-fast timeout: on a dev machine with no robot
# attached, or the wrong device on the matched port, the bus open / motor
# handshake can block indefinitely. A bus_factory that never returns stands
# in for that; connect() must give up after connect_timeout instead of
# hanging, naming the port in a clear, actionable error.
# ----------------------------------------------------------------------


def test_connect_times_out_when_bus_never_responds() -> None:
    """A bus_factory that never returns makes connect() raise DynamixelConnectTimeoutError
    within roughly connect_timeout, instead of hanging forever."""
    never_return = threading.Event()  # never set -> factory blocks "forever"

    def hanging_factory(*_args: Any, **_kwargs: Any) -> Any:
        never_return.wait()  # simulates an unresponsive port-open / handshake
        return FakeBus()  # pragma: no cover - unreachable within the test's lifetime

    driver = DynamixelDriver("/dev/fake", bus_factory=hanging_factory, connect_timeout=0.05)

    start = time.perf_counter()
    with pytest.raises(DynamixelConnectTimeoutError, match="/dev/fake"):
        driver.connect()
    elapsed = time.perf_counter() - start

    assert elapsed < 2.0, "connect() should fail fast, not hang"
    assert not driver.is_connected


def test_connect_timeout_error_names_device_and_port() -> None:
    """The timeout error names the Dynamixel servo bus and the port it was probing."""
    never_return = threading.Event()

    def hanging_factory(*_args: Any, **_kwargs: Any) -> Any:
        never_return.wait()
        return FakeBus()  # pragma: no cover

    driver = DynamixelDriver("/dev/ttyACM7", bus_factory=hanging_factory, connect_timeout=0.05)
    with pytest.raises(DynamixelConnectTimeoutError, match="servo bus") as exc_info:
        driver.connect()
    assert "/dev/ttyACM7" in str(exc_info.value)


def test_connect_within_timeout_succeeds_normally() -> None:
    """A bus_factory that returns promptly is unaffected by the connect_timeout guard."""
    driver, _bus, calls = make_driver(connect_timeout=0.05)
    driver.connect()
    assert driver.is_connected
    assert len(calls) == 1


def test_connect_timeout_leaves_driver_disconnected_for_rollback() -> None:
    """A connect timeout leaves is_connected False, so Palmimo.connect()'s rollback
    (which gates on driver_connected) correctly treats this as a failed connect."""
    never_return = threading.Event()

    def hanging_factory(*_args: Any, **_kwargs: Any) -> Any:
        never_return.wait()
        return FakeBus()  # pragma: no cover

    driver = DynamixelDriver("/dev/fake", bus_factory=hanging_factory, connect_timeout=0.05)
    robot = Palmimo(driver=driver, auto_wake=False)
    with pytest.raises(DynamixelConnectTimeoutError):
        robot.connect()
    assert not robot.is_connected
    assert not driver.is_connected


def test_connect_timeout_error_names_auto_detected_port_when_resolved_before_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """port=None: if auto-detection resolves a port before the bus_factory itself hangs,
    the timeout error names that resolved port instead of a generic placeholder."""
    monkeypatch.setattr("palmimo_sdk.io.dynamixel.find_servo_port", lambda: "/dev/detected9")
    never_return = threading.Event()

    def hanging_factory(*_args: Any, **_kwargs: Any) -> Any:
        never_return.wait()
        return FakeBus()  # pragma: no cover

    driver = DynamixelDriver(bus_factory=hanging_factory, connect_timeout=0.05)
    with pytest.raises(DynamixelConnectTimeoutError, match="/dev/detected9"):
        driver.connect()


def test_connect_timeout_closes_late_arriving_bus_once_worker_unblocks() -> None:
    """Late-arriving bus: the bus_factory can eventually finish successfully (fully
    armed: torque disable/config/hold-pose/torque enable) after connect() already gave
    up and raised DynamixelConnectTimeoutError. That bus is orphaned -- driver._bus was
    never assigned, so it's invisible to any rollback -- and must be closed (torque cut)
    rather than leaked open to race a caller's retry for the same port."""
    release = threading.Event()
    bus = FakeBus()

    def hanging_factory(*_args: Any, **_kwargs: Any) -> Any:
        release.wait()  # blocks past the deadline until the test unblocks it
        return bus

    driver = DynamixelDriver("/dev/fake", bus_factory=hanging_factory, connect_timeout=0.05)

    with pytest.raises(DynamixelConnectTimeoutError):
        driver.connect()
    assert bus.disconnect_calls == []  # not yet -- the worker is still blocked
    assert not driver.is_connected  # the failed connect owns nothing

    release.set()  # let the abandoned worker finish, fully "arming" the bus
    deadline = time.perf_counter() + 2.0
    while not bus.disconnect_calls and time.perf_counter() < deadline:
        time.sleep(0.01)

    assert bus.disconnect_calls == [True], "the orphaned, late-arriving bus should be closed (torque cut)"
    assert not driver.is_connected  # the late cleanup never assigns driver._bus


def test_read_telemetry_returns_each_signal_in_its_own_unit() -> None:
    """Current stays raw; voltage becomes volts; temperature is already °C."""
    driver, bus, _ = make_driver()
    driver.connect()
    bus.telemetry["leg_1_yaw"] = {
        "Present_Current": 812,
        "Present_Input_Voltage": 47,
        "Present_Temperature": 41,
    }

    telemetry = driver.read_telemetry()

    assert telemetry.current["leg_1_yaw"] == 812
    assert telemetry.voltage["leg_1_yaw"] == pytest.approx(4.7)
    assert telemetry.temperature["leg_1_yaw"] == 41


def test_read_telemetry_reads_every_signal_in_one_sweep() -> None:
    """Three signals, one bus transaction — the whole point of the span read."""
    driver, bus, _ = make_driver()
    driver.connect()

    driver.read_telemetry()

    assert len(bus.span_reads) == 1
    fields, _motors = bus.span_reads[0]
    assert set(fields) == {"Present_Current", "Present_Input_Voltage", "Present_Temperature"}


def test_read_telemetry_omits_a_motor_that_did_not_answer() -> None:
    """No stand-in value: an invented current would read as evidence of health."""
    driver, bus, _ = make_driver()
    driver.connect()
    del bus.telemetry["leg_1_pitch1"]

    telemetry = driver.read_telemetry()

    assert "leg_1_pitch1" not in telemetry.current
    assert "leg_1_pitch1" not in telemetry.voltage
    assert "leg_1_pitch1" not in telemetry.temperature
    assert "leg_1_pitch1" in telemetry.silent


def test_read_telemetry_separates_unreached_motors_from_silent_ones() -> None:
    """Only the motor that stopped the sweep is charged with the failure."""
    driver, bus, _ = make_driver()
    driver.connect()
    del bus.telemetry["leg_1_pitch1"]

    telemetry = driver.read_telemetry()

    assert telemetry.silent == ("leg_1_pitch1",)
    assert "leg_1_pitch2" in telemetry.unreached
    assert "leg_1_yaw" in telemetry.current  # asked before the failure, so it answered


def test_read_telemetry_sweeps_only_the_requested_motors() -> None:
    """A caller that set a motor aside sweeps the rest, and re-checks on its own schedule."""
    driver, bus, _ = make_driver()
    driver.connect()

    telemetry = driver.read_telemetry(["leg_2_yaw"])

    assert set(telemetry.current) == {"leg_2_yaw"}
    assert bus.span_reads[0][1] == ("leg_2_yaw",)


def test_read_telemetry_requires_a_connection() -> None:
    driver, _bus, _ = make_driver()
    with pytest.raises(RuntimeError, match="not connected"):
        driver.read_telemetry()
