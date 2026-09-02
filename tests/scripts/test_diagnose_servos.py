"""Tests for the supported DYNAMIXEL diagnostics CLI."""

from __future__ import annotations

import argparse
import builtins
import importlib.util
import math
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest


_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "diagnose_servos.py"
_SDK_DYNAMIXEL_PATH = Path(__file__).parents[2] / "packages" / "palmimo_sdk" / "palmimo_sdk" / "io" / "dynamixel.py"
_SPEC = importlib.util.spec_from_file_location("diagnose_servos", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
diagnose_servos = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(diagnose_servos)


class _Progress:
    def __init__(self, values: range) -> None:
        self._values = values

    def __enter__(self) -> _Progress:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __iter__(self) -> Iterator[int]:
        return iter(self._values)

    def set_postfix(self, **values: Any) -> None:
        pass


def test_scan_dynamixel_pings_each_id_once(monkeypatch: Any) -> None:
    calls: list[int] = []

    class _PortHandler:
        def __init__(self, port: str) -> None:
            pass

        def openPort(self) -> bool:  # noqa: N802 - mirrors the vendor API
            return True

        def setBaudRate(self, baudrate: int) -> bool:  # noqa: N802 - mirrors the vendor API
            return True

        def closePort(self) -> None:  # noqa: N802 - mirrors the vendor API
            pass

    class _PacketHandler:
        def __init__(self, protocol: float) -> None:
            pass

        def ping(self, port_handler: Any, dxl_id: int) -> tuple[int, int, int]:
            calls.append(dxl_id)
            return 1000 + dxl_id, 0 if dxl_id == 2 else 1, 0

    monkeypatch.setattr(diagnose_servos, "PortHandler", _PortHandler)
    monkeypatch.setattr(diagnose_servos, "PacketHandler", _PacketHandler)
    monkeypatch.setattr(diagnose_servos, "tqdm", lambda values, **kwargs: _Progress(values))

    assert diagnose_servos.scan_dynamixel("test-port", 1_000_000, 1, 3) == [(2, 1002)]
    assert calls == [1, 2, 3]


class _FakeBus:
    """A servo's register file and its joint, recording every transaction performed.

    The joint is modelled, not just the registers, because the defect this file
    has to be able to express is a servo that accepts every write, answers every
    read honestly, and never moves. A register file alone cannot tell that apart
    from a working servo: both look identical on the wire. So Present_Position
    advances toward Goal_Position during each dwell, by *follow* of the distance
    left, and only under the two conditions that actually drive a real joint --
    torque on, and Position_P_Gain at or above *min_follow_gain*. ``follow=1.0``
    with ``min_follow_gain=0`` is a servo that tracks perfectly; ``follow=0.0``
    is the dead joint; a gain floor is the soft-released neck. *joint_model*
    replaces that rule outright for the joints it cannot express -- one that
    tracks a target and then jams, one that sits at a fixed offset however it is
    commanded, one that moves the wrong way.

    Failure injection mirrors how this bus really fails: a dropped packet (comm
    result != 0), a write the servo rejects in the status packet's error byte
    while comm still reports success, a servo whose hardware error has latched
    (the alert bit on every packet it sends, reads and writes alike, while it
    keeps answering and obeying), and a serial layer that raises once the cable
    is gone.
    """

    def __init__(
        self,
        registers: dict[int, int],
        read_comm_fail: set[int] | None = None,
        read_raises: set[int] | None = None,
        write_error: dict[int, int] | None = None,
        write_raises: dict[int, int] | None = None,
        raises_type: type[BaseException] = OSError,
        latched: bool = False,
        latch_on_write: tuple[int, int] | None = None,
        sleep_raises: int | None = None,
        follow: float = 1.0,
        min_follow_gain: int = 0,
        joint_model: Callable[[int, int], int] | None = None,
    ) -> None:
        self.registers = dict(registers)
        self.log: list[tuple[str, int, int]] = []
        self.slept: list[float] = []
        self.closed = False
        self._read_comm_fail = read_comm_fail or set()
        self._read_raises = read_raises or set()
        self._write_error = write_error or {}
        self._write_raises = write_raises or {}
        self._raises_type = raises_type
        self._write_counts: dict[int, int] = {}
        self._latched = latched
        self._latch_on_write = latch_on_write
        self._sleep_raises = sleep_raises
        self._sleeps = 0
        self._follow = follow
        self._min_follow_gain = min_follow_gain
        self._joint_model = joint_model

    def _alert(self) -> int:
        """Return the alert bit this packet carries: bit 7, set while the error is latched."""
        return diagnose_servos.ERRBIT_ALERT if self._latched else 0

    def read(self, address: int) -> tuple[int, int, int]:
        """Return the vendor API's (value, comm result, error byte) for one read."""
        if address in self._read_raises:
            raise self._raises_type(f"port read of {address} interrupted")
        value = self.registers.get(address, 0)
        self.log.append(("read", address, value))
        if address in self._read_comm_fail:
            return 0, -3001, 0
        return value, 0, self._alert()

    def write(self, address: int, value: int) -> tuple[int, int]:
        """Return the vendor API's (comm result, error byte) for one write."""
        count = self._write_counts.get(address, 0) + 1
        self._write_counts[address] = count
        if self._write_raises.get(address) == count:
            raise self._raises_type(f"port write to {address} interrupted")
        if self._latch_on_write == (address, count):
            # The servo latches while carrying out this write: it obeys, and the
            # status packet it sends back is the first to carry the alert bit.
            self._latched = True
        self.log.append(("write", address, value))
        if self._write_error.get(address) == count:
            # Protocol 2.0: comm succeeded, the servo refused the value.
            return 0, 0x07 | self._alert()
        self.registers[address] = value
        return 0, self._alert()

    def sleep(self, seconds: float) -> None:
        """Stand in for ``time.sleep``, moving the joint over the dwell it stands for.

        The dwell is when a real joint travels, so it is where the model moves
        too: a reading taken after this call reports where the joint got to,
        which is the whole observation the tool produces.
        """
        self._sleeps += 1
        if self._sleeps == self._sleep_raises:
            raise self._raises_type("interrupted during the dwell")
        self.slept.append(seconds)
        self.log.append(("sleep", 0, 0))
        self._advance_joint()

    def _advance_joint(self) -> None:
        """Move Present_Position toward Goal_Position, if anything is driving it."""
        if self.registers.get(diagnose_servos.ADDR_TORQUE_ENABLE) != 1:
            return
        if self.registers.get(diagnose_servos.ADDR_POSITION_P_GAIN, 0) < self._min_follow_gain:
            return
        present = self.registers.get(diagnose_servos.ADDR_PRESENT_POSITION, 0)
        goal = self.registers.get(diagnose_servos.ADDR_GOAL_POSITION, present)
        if self._joint_model is not None:
            self.registers[diagnose_servos.ADDR_PRESENT_POSITION] = self._joint_model(present, goal)
            return
        self.registers[diagnose_servos.ADDR_PRESENT_POSITION] = present + round(self._follow * (goal - present))

    def writes(self, address: int) -> list[int]:
        """Values written to one register, in order."""
        return [value for kind, addr, value in self.log if kind == "write" and addr == address]

    def written_addresses(self) -> list[int]:
        """Every written register address, in order."""
        return [addr for kind, addr, _ in self.log if kind == "write"]

    def write_log(self) -> list[tuple[int, int]]:
        """Every (address, value) written, in order."""
        return [(addr, value) for kind, addr, value in self.log if kind == "write"]

    def reads(self, address: int) -> int:
        """How many times one register was read."""
        return sum(1 for kind, addr, _ in self.log if kind == "read" and addr == address)


# A servo as a freshly powered robot reports it: position control, no profile
# (Profile_Velocity/Acceleration = 0), and limits narrower than the safe range.
_BASE_REGISTERS = {
    diagnose_servos.ADDR_DRIVE_MODE: 0x05,  # reversed joint, time-based profile
    diagnose_servos.ADDR_OPERATING_MODE: diagnose_servos.OPERATING_MODE_POSITION,
    diagnose_servos.ADDR_MIN_POSITION_LIMIT: 1023,
    diagnose_servos.ADDR_MAX_POSITION_LIMIT: 3073,
    diagnose_servos.ADDR_TORQUE_ENABLE: 0,
    diagnose_servos.ADDR_PROFILE_ACCELERATION: 0,
    diagnose_servos.ADDR_PROFILE_VELOCITY: 0,
    diagnose_servos.ADDR_PRESENT_POSITION: 1391,
    diagnose_servos.ADDR_PRESENT_TEMPERATURE: 34,
    # A leg: the SDK's default gain, which its neck soft-release never touched.
    diagnose_servos.ADDR_POSITION_P_GAIN: 900,
}

_START = _BASE_REGISTERS[diagnose_servos.ADDR_PRESENT_POSITION]

# The gain a neck servo carries after any ordinary SDK session: the last step of
# the shutdown park's soft-release ramp, measured on ids 19-21 of a real robot.
_SOFT_RELEASED_NECK_GAIN = 6
# The per-dwell follow rate that puts the first reading at the 130 of 170 units
# a neck holding the head against gravity was measured at. It is a first-order
# lag, not a model of that neck: `_advance_joint` applies it to the distance
# LEFT, so the joint keeps closing on the goal and settles at ~0.62 of each
# commanded excursion rather than 0.76. A real gravity-loaded joint settles at a
# fixed offset from its goal instead -- the shape `_sits_at` expresses, and the
# shape that passed the distance-from-start check while following nothing.
_LOADED_NECK_FOLLOW = 130 / 170
# Written out rather than derived from the constants under test: the pair of
# tests either side of the allowance pins MIN_TRACKING_FRACTION at 0.5 only
# while these numbers are literals. Half of a +/-170 unit swing is 85 units.
_SWING_UNITS = 170
_ALLOWED_ERROR_UNITS = 85


def _sits_at(position: int) -> Callable[[int, int], int]:
    """A joint that holds one position whatever it is commanded."""
    return lambda present, goal: position


def _jams_after_one_target() -> Callable[[int, int], int]:
    """A joint that tracks the first target it is given and then never moves again."""
    tracked = False

    def model(present: int, goal: int) -> int:
        nonlocal tracked
        if tracked:
            return present
        tracked = True
        return goal

    return model


def _lags_by(error: int, start: int = _START) -> Callable[[int, int], int]:
    """A joint that stops *error* units short of every target, on both sides of *start*."""

    def model(present: int, goal: int) -> int:
        if goal == start:
            return start
        return goal - error if goal > start else goal + error

    return model


def _moves_the_wrong_way(distance: int, start: int = _START) -> Callable[[int, int], int]:
    """A joint that travels *distance* units away from every target it is given."""
    return lambda present, goal: start - distance if goal > start else start + distance


def _install_fake_bus(
    monkeypatch: Any,
    overrides: dict[int, int] | None = None,
    read_comm_fail: set[int] | None = None,
    read_raises: set[int] | None = None,
    write_error: dict[int, int] | None = None,
    write_raises: dict[int, int] | None = None,
    raises_type: type[BaseException] = OSError,
    latched: bool = False,
    latch_on_write: tuple[int, int] | None = None,
    sleep_raises: int | None = None,
    follow: float = 1.0,
    min_follow_gain: int = 0,
    joint_model: Callable[[int, int], int] | None = None,
) -> _FakeBus:
    """Patch the vendor handlers with a fake bus and return its recorder.

    Args:
        monkeypatch: pytest's patcher.
        overrides: Register values replacing :data:`_BASE_REGISTERS`.
        read_comm_fail: Addresses whose every read drops the packet.
        read_raises: Addresses whose read raises like a dead port.
        write_error: Address -> which write (1-based) the servo rejects.
        write_raises: Address -> which write (1-based) raises like a dead port.
        raises_type: What *write_raises*/*sleep_raises* raise (``KeyboardInterrupt`` for Ctrl+C).
        latched: Whether the servo's hardware error is already latched.
        latch_on_write: (address, write count) at which the servo latches one.
        sleep_raises: Which dwell (1-based) raises, standing in for a Ctrl+C during it.
        follow: Fraction of the remaining distance to Goal_Position the joint
            covers per dwell. ``0.0`` is a joint that never moves.
        min_follow_gain: Position_P_Gain below which the joint does not move at
            all, however hard it is commanded.
        joint_model: (present, goal) -> new present, replacing *follow* for the
            joints that rule cannot express.
    """
    bus = _FakeBus(
        {**_BASE_REGISTERS, **(overrides or {})},
        read_comm_fail=read_comm_fail,
        read_raises=read_raises,
        write_error=write_error,
        write_raises=write_raises,
        raises_type=raises_type,
        latched=latched,
        latch_on_write=latch_on_write,
        sleep_raises=sleep_raises,
        follow=follow,
        min_follow_gain=min_follow_gain,
        joint_model=joint_model,
    )

    class _PortHandler:
        def __init__(self, port: str) -> None:
            pass

        def openPort(self) -> bool:  # noqa: N802 - mirrors the vendor API
            return True

        def setBaudRate(self, baudrate: int) -> bool:  # noqa: N802 - mirrors the vendor API
            return True

        def closePort(self) -> None:  # noqa: N802 - mirrors the vendor API
            bus.closed = True

    class _PacketHandler:
        def __init__(self, protocol: float) -> None:
            pass

        def ping(self, port_handler: Any, dxl_id: int) -> tuple[int, int, int]:
            return 1240, 0, 0

        def read1ByteTxRx(self, port: Any, dxl_id: int, address: int) -> tuple[int, int, int]:  # noqa: N802
            return bus.read(address)

        def read2ByteTxRx(self, port: Any, dxl_id: int, address: int) -> tuple[int, int, int]:  # noqa: N802
            return bus.read(address)

        def read4ByteTxRx(self, port: Any, dxl_id: int, address: int) -> tuple[int, int, int]:  # noqa: N802
            return bus.read(address)

        def write1ByteTxRx(self, port: Any, dxl_id: int, address: int, value: int) -> tuple[int, int]:  # noqa: N802
            return bus.write(address, value)

        def write2ByteTxRx(self, port: Any, dxl_id: int, address: int, value: int) -> tuple[int, int]:  # noqa: N802
            return bus.write(address, value)

        def write4ByteTxRx(self, port: Any, dxl_id: int, address: int, value: int) -> tuple[int, int]:  # noqa: N802
            return bus.write(address, value)

        def getTxRxResult(self, comm: int) -> str:  # noqa: N802 - mirrors the vendor API
            return f"comm {comm}"

        def getRxPacketError(self, error: int) -> str:  # noqa: N802 - mirrors the vendor API
            return f"error 0x{error:02x}"

    monkeypatch.setattr(diagnose_servos, "PortHandler", _PortHandler)
    monkeypatch.setattr(diagnose_servos, "PacketHandler", _PacketHandler)
    monkeypatch.setattr(diagnose_servos, "time", bus)
    return bus


def test_oscillate_servo_centers_the_swing_on_the_present_position(monkeypatch: Any) -> None:
    bus = _install_fake_bus(monkeypatch)

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    goals = bus.writes(diagnose_servos.ADDR_GOAL_POSITION)
    present = _BASE_REGISTERS[diagnose_servos.ADDR_PRESENT_POSITION]
    # The goal is seeded with the present position so enabling torque holds the
    # pose, and the first swing is one amplitude away from there — not from 2048.
    assert goals[0] == present
    assert goals[1] == present + diagnose_servos.OSCILLATION_AMPLITUDE
    assert goals[-1] == present
    fixed_center_target = diagnose_servos.DXL_CENTER_POSITION + diagnose_servos.OSCILLATION_AMPLITUDE
    assert fixed_center_target not in goals


def test_oscillate_servo_writes_a_profile_before_any_goal_position(monkeypatch: Any) -> None:
    bus = _install_fake_bus(monkeypatch)

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    written = bus.written_addresses()
    first_goal = written.index(diagnose_servos.ADDR_GOAL_POSITION)
    assert written.index(diagnose_servos.ADDR_PROFILE_ACCELERATION) < first_goal
    assert written.index(diagnose_servos.ADDR_PROFILE_VELOCITY) < first_goal
    # Time-based Drive_Mode: both registers are durations in milliseconds.
    assert bus.writes(diagnose_servos.ADDR_PROFILE_VELOCITY)[0] == diagnose_servos.PROFILE_VELOCITY_MS
    assert bus.writes(diagnose_servos.ADDR_PROFILE_ACCELERATION)[0] == diagnose_servos.PROFILE_ACCELERATION_MS
    # The servo requires the ramp to be at most half the move time.
    assert diagnose_servos.PROFILE_ACCELERATION_MS * 2 <= diagnose_servos.PROFILE_VELOCITY_MS


def test_oscillate_servo_writes_velocity_units_when_the_drive_mode_is_not_time_based(monkeypatch: Any) -> None:
    bus = _install_fake_bus(monkeypatch, {diagnose_servos.ADDR_DRIVE_MODE: 0x01})

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    profile = diagnose_servos._profile_registers(time_based=False)
    # A velocity-based servo reads Profile_Velocity in ~0.229 rpm units, so the
    # millisecond value would mean full speed rather than a ramp.
    assert profile.velocity != diagnose_servos.PROFILE_VELOCITY_MS
    assert bus.writes(diagnose_servos.ADDR_PROFILE_VELOCITY)[0] == profile.velocity
    assert bus.writes(diagnose_servos.ADDR_PROFILE_ACCELERATION)[0] == profile.acceleration


def test_oscillate_servo_dwells_longer_than_the_profiled_move_in_both_drive_modes(monkeypatch: Any) -> None:
    # The dwell is what makes a printed reading mean anything: command a target,
    # wait out the move, then read. A dwell shorter than the profiled move reads
    # a joint still in flight and reports it as where the joint got to. The two
    # drive modes take different times for the same speed setting, so no single
    # fixed period is correct for both.
    for drive_mode, time_based in ((0x05, True), (0x01, False)):
        bus = _install_fake_bus(monkeypatch, {diagnose_servos.ADDR_DRIVE_MODE: drive_mode})

        assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 5.0) is True

        move_seconds = diagnose_servos._profile_registers(time_based=time_based).move_seconds
        assert bus.slept, "the loop must dwell between commanding a target and reading it back"
        assert min(bus.slept) > move_seconds


def test_velocity_based_profile_reports_the_move_time_its_registers_imply() -> None:
    # Recomputed here straight from the control-table units, so a wrong unit or a
    # move time quietly copied from the time-based branch does not pass.
    profile = diagnose_servos._profile_registers(time_based=False)
    peak_rev_per_second = profile.velocity * 0.229 / 60.0
    accel_rev_per_second2 = profile.acceleration * 214.577 / 3600.0
    travel_rev = 2 * diagnose_servos.OSCILLATION_AMPLITUDE / diagnose_servos.TICKS_PER_REV
    ramp_seconds = peak_rev_per_second / accel_rev_per_second2
    ramp_rev = peak_rev_per_second * ramp_seconds

    if ramp_rev >= travel_rev:
        expected = 2 * math.sqrt(travel_rev / accel_rev_per_second2)
    else:
        expected = 2 * ramp_seconds + (travel_rev - ramp_rev) / peak_rev_per_second
    assert profile.move_seconds == pytest.approx(expected)
    # Slower than the time-based branch at the same setting, which is why the
    # dwell has to come from the profile rather than from one shared constant.
    assert profile.move_seconds > diagnose_servos._profile_registers(time_based=True).move_seconds


def test_default_duration_swings_more_often_than_the_two_swing_floor(monkeypatch: Any) -> None:
    # `--duration` is divided by the dwell, so a slower profile means fewer
    # swings for the same request. At the default the count must still come from
    # the duration, not from the floor that keeps `--duration 0.1` visible.
    bus = _install_fake_bus(monkeypatch)

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, diagnose_servos.DEFAULT_DURATION) is True

    # Goal writes are the seed, one per swing, and the return to the start.
    swings = len(bus.writes(diagnose_servos.ADDR_GOAL_POSITION)) - 2
    period = diagnose_servos._swing_period_seconds(diagnose_servos._profile_registers(time_based=True))
    assert swings == round(diagnose_servos.DEFAULT_DURATION / period)
    assert swings > 2


def test_oscillate_servo_clamps_targets_to_the_servo_position_limits(monkeypatch: Any) -> None:
    low_limit = _BASE_REGISTERS[diagnose_servos.ADDR_MIN_POSITION_LIMIT]
    high_limit = _BASE_REGISTERS[diagnose_servos.ADDR_MAX_POSITION_LIMIT]
    bus = _install_fake_bus(monkeypatch, {diagnose_servos.ADDR_PRESENT_POSITION: high_limit - 23})

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    goals = bus.writes(diagnose_servos.ADDR_GOAL_POSITION)
    assert all(low_limit <= goal <= high_limit for goal in goals)
    assert max(goals) == high_limit  # clamped, not present + amplitude


def test_oscillate_servo_clamps_targets_to_the_safe_range_when_the_servo_limits_are_wider(monkeypatch: Any) -> None:
    bus = _install_fake_bus(
        monkeypatch,
        {
            diagnose_servos.ADDR_MIN_POSITION_LIMIT: 0,
            diagnose_servos.ADDR_MAX_POSITION_LIMIT: 4095,
            diagnose_servos.ADDR_PRESENT_POSITION: 3850,
        },
    )

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    goals = bus.writes(diagnose_servos.ADDR_GOAL_POSITION)
    assert max(goals) == diagnose_servos.SAFE_MAX_TICK


def test_oscillate_servo_refuses_when_the_joint_has_no_room_to_swing(monkeypatch: Any) -> None:
    bus = _install_fake_bus(
        monkeypatch,
        {
            diagnose_servos.ADDR_MIN_POSITION_LIMIT: 3050,
            diagnose_servos.ADDR_MAX_POSITION_LIMIT: 3073,
            diagnose_servos.ADDR_PRESENT_POSITION: 3073,
        },
    )

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    # The refusal is decided from reads alone, so the servo is left untouched.
    assert bus.written_addresses() == []


def test_oscillate_servo_restores_the_profile_it_found(monkeypatch: Any) -> None:
    bus = _install_fake_bus(monkeypatch)

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    assert bus.writes(diagnose_servos.ADDR_PROFILE_VELOCITY)[-1] == 0
    assert bus.writes(diagnose_servos.ADDR_PROFILE_ACCELERATION)[-1] == 0
    assert bus.registers[diagnose_servos.ADDR_PROFILE_VELOCITY] == 0
    assert bus.registers[diagnose_servos.ADDR_PROFILE_ACCELERATION] == 0


def test_oscillate_servo_seeds_the_goal_before_enabling_torque(monkeypatch: Any) -> None:
    bus = _install_fake_bus(monkeypatch)

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    # Enabling torque first would make the servo snap to whatever Goal_Position
    # it still carried — the exact jolt this whole feature exists to prevent.
    writes = bus.write_log()
    seed = next(i for i, (addr, _) in enumerate(writes) if addr == diagnose_servos.ADDR_GOAL_POSITION)
    torque_on = next(
        i for i, (addr, value) in enumerate(writes) if addr == diagnose_servos.ADDR_TORQUE_ENABLE and value == 1
    )
    assert seed < torque_on
    assert writes[seed][1] == _BASE_REGISTERS[diagnose_servos.ADDR_PRESENT_POSITION]


def test_oscillate_servo_fails_when_the_joint_never_moves(monkeypatch: Any, capsys: Any) -> None:
    # The defect measured on a 21-servo unit: every write lands, every read is
    # honest, the joint sits at its starting position through all four swings,
    # and the run reports success. Nothing on the bus distinguishes this from a
    # working servo, so the only evidence is how far the joint got.
    bus = _install_fake_bus(monkeypatch, follow=0.0)

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    out = capsys.readouterr().out
    # Nothing on the bus went wrong -- that is what makes this defect invisible
    # to every other check in the file, and what the fake bus has to reproduce.
    assert "rejected" not in out
    assert "Failed to" not in out
    assert "Torque disabled." in out
    # What was commanded, and how far from it the joint was.
    assert f"+/-{diagnose_servos.OSCILLATION_AMPLITUDE} units" in out
    assert f"read {diagnose_servos.OSCILLATION_AMPLITUDE} units" in out
    # The gain is ruled out rather than offered as the first thing to check: the
    # swing this reading came from was already driven at it.
    assert f"driven at Position_P_Gain {diagnose_servos.SWING_POSITION_P_GAIN}" in out
    assert "not the cause" in out
    assert out.isascii()
    # The failed verdict does not skip the exit path: parked, released, restored.
    assert bus.writes(diagnose_servos.ADDR_TORQUE_ENABLE)[-1] == 0
    assert bus.registers[diagnose_servos.ADDR_PROFILE_VELOCITY] == 0


def test_oscillate_servo_fails_a_joint_that_barely_twitches(monkeypatch: Any) -> None:
    # Not only a joint pinned at exactly zero: one dragged a tenth of the way is
    # not being driven either, and 17 of 170 units is not the observation the
    # command exists to produce.
    _install_fake_bus(monkeypatch, follow=0.1)

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False


def test_report_travel_never_passes_a_joint_that_did_not_move(capsys: Any) -> None:
    # A zero commanded excursion cannot reach the verdict today (MIN_VISIBLE_TRAVEL
    # guarantees the swing is bigger than that), and against a zero target a joint
    # that never left its start has zero tracking error -- so the tracking rule
    # alone would call it followed. The claim the command sells is "that joint
    # moved", which is what the absolute floor keeps true at any commanded size.
    assert diagnose_servos._report_travel(2, 0, 0, 0) is False
    assert capsys.readouterr().out.isascii()


def test_oscillate_servo_fails_a_joint_that_tracks_one_target_and_then_jams(monkeypatch: Any, capsys: Any) -> None:
    # It reaches the first target exactly and never moves again: it never
    # oscillates, and it never comes back to where it started. Judged on how far
    # it ever got from its starting position it reads a full commanded
    # excursion, the best score there is, so only the target it was given at the
    # time tells this apart from a working joint.
    _install_fake_bus(monkeypatch, joint_model=_jams_after_one_target())

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    out = capsys.readouterr().out
    assert "did not follow" in out
    assert out.isascii()


def test_oscillate_servo_fails_a_joint_that_sags_at_torque_on_and_ignores_every_goal(
    monkeypatch: Any, capsys: Any
) -> None:
    # The failure this check exists to catch, in its real physical shape: a
    # proportional controller too soft for the load settles at a fixed offset
    # from its goal, so the joint drops the moment torque is enabled and stays
    # there through every command. The drop is 120 units of the 170 commanded --
    # 0.71 of the excursion, and in the OPPOSITE direction to the first target.
    _install_fake_bus(monkeypatch, joint_model=_sits_at(_START - 120))

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    out = capsys.readouterr().out
    assert "did not follow" in out
    assert out.isascii()


def test_oscillate_servo_fails_a_joint_that_moves_exactly_out_of_phase(monkeypatch: Any, capsys: Any) -> None:
    # Right amplitude, wrong direction, every time -- a servo wired or mounted
    # against the joint it drives. It is as far from following as a dead joint,
    # and the distance it covers is what makes that invisible to any rule
    # measured from the starting position.
    _install_fake_bus(monkeypatch, joint_model=_moves_the_wrong_way(100))

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    out = capsys.readouterr().out
    assert "did not follow" in out
    assert out.isascii()


def test_oscillate_servo_passes_a_joint_that_lags_just_inside_the_tracking_allowance(monkeypatch: Any) -> None:
    # This test and the one below it are what make MIN_TRACKING_FRACTION = 0.5 a
    # fact: 84 units of lag on a +/-170 unit swing passes and 86 fails, which no
    # other value of the fraction does. The amplitude is asserted for the same
    # reason -- the pair pins nothing if the swing it is measured against moves.
    assert diagnose_servos.OSCILLATION_AMPLITUDE == _SWING_UNITS
    _install_fake_bus(monkeypatch, joint_model=_lags_by(_ALLOWED_ERROR_UNITS - 1))

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True


def test_oscillate_servo_fails_a_joint_that_lags_just_outside_the_tracking_allowance(
    monkeypatch: Any, capsys: Any
) -> None:
    assert diagnose_servos.OSCILLATION_AMPLITUDE == _SWING_UNITS
    _install_fake_bus(monkeypatch, joint_model=_lags_by(_ALLOWED_ERROR_UNITS + 1))

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    # The reading, the allowance it broke, and no other explanation offered.
    out = capsys.readouterr().out
    assert f"read {_ALLOWED_ERROR_UNITS + 1} units" in out
    assert f"past the {_ALLOWED_ERROR_UNITS} units" in out


def test_oscillate_servo_fails_a_joint_that_barely_moves_inside_a_clamped_swing(monkeypatch: Any, capsys: Any) -> None:
    # Position limits 60 units apart, the narrowest window this command runs at
    # all, with the joint 7 units off the low limit: the commanded excursion is
    # 53 units, so the tracking allowance shrinks to 26. A joint lagging 26 units
    # covers 8 units peak to peak -- 0.7 degrees, and a pass on that says nothing
    # about which joint the servo drives. The absolute floor is what fails it.
    _install_fake_bus(
        monkeypatch,
        {
            diagnose_servos.ADDR_MIN_POSITION_LIMIT: 1023,
            diagnose_servos.ADDR_MAX_POSITION_LIMIT: 1083,
            diagnose_servos.ADDR_PRESENT_POSITION: 1030,
        },
        joint_model=_lags_by(26, start=1030),
    )

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    out = capsys.readouterr().out
    assert f"under the {diagnose_servos.MIN_TRACKING_SPAN} units" in out
    assert "nearer the middle of its range" in out
    assert out.isascii()


def test_oscillate_servo_fails_a_sagging_joint_inside_a_clamped_swing(monkeypatch: Any) -> None:
    # The same narrow window, centered: half of a 30-unit excursion is a 15-unit
    # pass floor, which a dead joint sagging 20 units clears without ever
    # following anything.
    _install_fake_bus(
        monkeypatch,
        {
            diagnose_servos.ADDR_MIN_POSITION_LIMIT: 1023,
            diagnose_servos.ADDR_MAX_POSITION_LIMIT: 1083,
            diagnose_servos.ADDR_PRESENT_POSITION: 1053,
        },
        joint_model=_sits_at(1033),
    )

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False


def test_swing_gain_is_the_sdk_default_the_constant_claims_it_is() -> None:
    # SWING_POSITION_P_GAIN is documented as the SDK's own power-on default, and
    # both the safety argument for writing it and the remedy printed on a failure
    # rest on that. Nothing else notices if either side moves: any gain above
    # ~100 passes every other test in this file, 16383 (maximum stiffness)
    # included. Read out of the SDK's source rather than imported, because
    # palmimo_sdk.io is an internal layer this tree may not reach into (ruff
    # TID251) -- and asserted found first, so a rename cannot turn this drift
    # check into a test that quietly stops checking.
    match = re.search(
        r"^_DEFAULT_POSITION_P_GAIN = (\d+)$", _SDK_DYNAMIXEL_PATH.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert match is not None, f"_DEFAULT_POSITION_P_GAIN not found in {_SDK_DYNAMIXEL_PATH}"
    assert int(match.group(1)) == diagnose_servos.SWING_POSITION_P_GAIN


def test_oscillate_servo_passes_a_loaded_joint_that_undershoots(monkeypatch: Any, capsys: Any) -> None:
    # The neck holding the head against gravity at Position_D_Gain 0 settles
    # where its proportional term balances the gravity torque: 130 units of the
    # 170 commanded, measured. That is a working joint, and a floor set too high
    # would report the robot's own neck as broken every time.
    _install_fake_bus(monkeypatch, follow=_LOADED_NECK_FOLLOW)

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    # The run really did undershoot -- 130 of the 170 units commanded -- so this
    # pins the floor from below, not just that some passing run passes.
    out = capsys.readouterr().out
    assert "from start: +130" in out
    assert "did not follow" not in out


def test_oscillate_servo_raises_the_position_p_gain_so_a_soft_released_neck_can_move(monkeypatch: Any) -> None:
    # The triggering sequence is ordinary: run the robot, then diagnose the neck.
    # The SDK's shutdown park leaves ids 19-21 on gain 6, and this tool talks to
    # the bus directly, so without a gain of its own it commands a joint that
    # cannot follow. `min_follow_gain` is what makes the fake bus express that:
    # the joint moves only once something raises the gain.
    bus = _install_fake_bus(
        monkeypatch,
        {diagnose_servos.ADDR_POSITION_P_GAIN: _SOFT_RELEASED_NECK_GAIN},
        follow=_LOADED_NECK_FOLLOW,
        min_follow_gain=100,
    )

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    assert bus.writes(diagnose_servos.ADDR_POSITION_P_GAIN)[0] == diagnose_servos.SWING_POSITION_P_GAIN


def test_oscillate_servo_restores_the_position_p_gain_it_found(monkeypatch: Any) -> None:
    # A gain left raised outlives the run. The SDK re-applies its default at
    # connect, but a joint the operator deliberately left soft must not come
    # back stiff because a diagnostic looked at it.
    bus = _install_fake_bus(
        monkeypatch,
        {diagnose_servos.ADDR_POSITION_P_GAIN: _SOFT_RELEASED_NECK_GAIN},
        follow=_LOADED_NECK_FOLLOW,
        min_follow_gain=100,
    )

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    assert bus.writes(diagnose_servos.ADDR_POSITION_P_GAIN)[-1] == _SOFT_RELEASED_NECK_GAIN
    assert bus.registers[diagnose_servos.ADDR_POSITION_P_GAIN] == _SOFT_RELEASED_NECK_GAIN


def test_oscillate_servo_raises_the_gain_after_seeding_the_goal_and_before_enabling_torque(monkeypatch: Any) -> None:
    # The ordering IS the safety property. A raised gain reaches the motor only
    # through the torque enable, so the goal has to already hold the present
    # position by then: raising it first, on a servo that is still holding torque
    # from a previous session, multiplies the standing error against a stale goal
    # and hauls a drooping head to it at full speed.
    bus = _install_fake_bus(monkeypatch, {diagnose_servos.ADDR_TORQUE_ENABLE: 1})

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    writes = bus.write_log()
    seed = next(i for i, (addr, _) in enumerate(writes) if addr == diagnose_servos.ADDR_GOAL_POSITION)
    gain = next(
        i
        for i, (addr, value) in enumerate(writes)
        if addr == diagnose_servos.ADDR_POSITION_P_GAIN and value == diagnose_servos.SWING_POSITION_P_GAIN
    )
    torque_on = next(
        i for i, (addr, value) in enumerate(writes) if addr == diagnose_servos.ADDR_TORQUE_ENABLE and value == 1
    )
    assert seed < gain < torque_on


def test_oscillate_servo_reports_a_failed_gain_restore(monkeypatch: Any, capsys: Any) -> None:
    # Position_P_Gain write 1 raises the gain, write 2 is the restore in `finally`.
    _install_fake_bus(monkeypatch, write_error={diagnose_servos.ADDR_POSITION_P_GAIN: 2})

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    assert "restore Position_P_Gain" in capsys.readouterr().out


def test_oscillate_servo_restores_the_profile_even_when_the_gain_restore_raises(monkeypatch: Any, capsys: Any) -> None:
    # Position_P_Gain write 1 raises the gain, write 2 is the restore in
    # `finally`, and the port is gone by then. Under one shared `try` the raise
    # took the profile restore with it, leaving Profile_Acceleration on the servo
    # until a power cycle -- silently, since the run still exits 0.
    bus = _install_fake_bus(monkeypatch, write_raises={diagnose_servos.ADDR_POSITION_P_GAIN: 2})

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    assert bus.registers[diagnose_servos.ADDR_PROFILE_ACCELERATION] == 0
    assert bus.registers[diagnose_servos.ADDR_PROFILE_VELOCITY] == 0
    assert bus.closed is True
    # Named: which register was left behind, and at what value.
    out = capsys.readouterr().out
    assert (
        f"could not restore Position_P_Gain on servo 2, which is left at {diagnose_servos.SWING_POSITION_P_GAIN}" in out
    )
    assert out.isascii()


def test_oscillate_servo_restores_the_gain_even_when_the_profile_restore_raises(monkeypatch: Any, capsys: Any) -> None:
    # The other order, and the one the operator is likelier to notice: a gain
    # left raised is a joint that comes back stiff. Profile_Acceleration write 1
    # sets the profile, write 2 is the restore in `finally`.
    bus = _install_fake_bus(
        monkeypatch,
        {diagnose_servos.ADDR_POSITION_P_GAIN: _SOFT_RELEASED_NECK_GAIN},
        follow=_LOADED_NECK_FOLLOW,
        min_follow_gain=100,
        write_raises={diagnose_servos.ADDR_PROFILE_ACCELERATION: 2},
    )

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    assert bus.registers[diagnose_servos.ADDR_POSITION_P_GAIN] == _SOFT_RELEASED_NECK_GAIN
    # The warning says which registers were left behind and at what values, so
    # the operator knows what state the joint is in without re-reading the bus.
    out = capsys.readouterr().out
    assert "Profile_Acceleration/Profile_Velocity" in out
    profile = diagnose_servos._profile_registers(time_based=True)
    assert f"left at {profile.acceleration}/{profile.velocity}" in out
    assert out.isascii()


def test_oscillate_servo_holds_back_a_raising_gain_restore_when_the_release_was_refused(
    monkeypatch: Any, capsys: Any
) -> None:
    # A servo found on a gain above the one this run drove at (1300 is what the
    # SDK's wave motion leaves), and the torque release refused, so the joint may
    # still be powered. Writing 1300 back would stiffen it further -- Torque_Enable
    # write 1 is the enable, write 2 the release the servo rejects.
    bus = _install_fake_bus(
        monkeypatch,
        {diagnose_servos.ADDR_POSITION_P_GAIN: 1300},
        write_error={diagnose_servos.ADDR_TORQUE_ENABLE: 2},
    )

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    assert bus.registers[diagnose_servos.ADDR_POSITION_P_GAIN] == diagnose_servos.SWING_POSITION_P_GAIN
    out = capsys.readouterr().out
    assert "may still be powered" in out
    assert out.isascii()


def test_oscillate_servo_still_lowers_the_gain_when_the_release_was_refused(monkeypatch: Any) -> None:
    # The other direction of the same restore: putting a soft-released neck back
    # on gain 6 only makes a still-powered joint weaker, so the refused release
    # is no reason to leave it at 900.
    bus = _install_fake_bus(
        monkeypatch,
        {diagnose_servos.ADDR_POSITION_P_GAIN: _SOFT_RELEASED_NECK_GAIN},
        follow=_LOADED_NECK_FOLLOW,
        min_follow_gain=100,
        write_error={diagnose_servos.ADDR_TORQUE_ENABLE: 2},
    )

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    assert bus.registers[diagnose_servos.ADDR_POSITION_P_GAIN] == _SOFT_RELEASED_NECK_GAIN


def test_oscillate_servo_notes_the_gain_it_could_not_read(monkeypatch: Any, capsys: Any) -> None:
    # Unreadable, so unrestorable. It still runs: the value left behind is the
    # servo's power-on default, which the SDK re-applies at connect -- unlike
    # Profile_Acceleration, which nothing rewrites and which the operator is
    # therefore sent to a power cycle over.
    _install_fake_bus(monkeypatch, read_comm_fail={diagnose_servos.ADDR_POSITION_P_GAIN})

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    out = capsys.readouterr().out
    assert "Position_P_Gain was never read" in out
    assert "no power cycle is needed" in out


def test_oscillate_servo_refuses_when_the_drive_mode_read_drops(monkeypatch: Any, capsys: Any) -> None:
    bus = _install_fake_bus(monkeypatch, read_comm_fail={diagnose_servos.ADDR_DRIVE_MODE})

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    # Drive_Mode decides what Profile_Velocity means; guessing "velocity-based"
    # on a time-based servo is the un-ramped slam. Nothing may be written.
    assert bus.written_addresses() == []
    assert bus.reads(diagnose_servos.ADDR_DRIVE_MODE) == diagnose_servos.READ_ATTEMPTS
    out = capsys.readouterr().out
    assert "Drive_Mode" in out
    assert "nothing was written" in out


def test_oscillate_servo_refuses_when_a_position_limit_read_drops(monkeypatch: Any, capsys: Any) -> None:
    bus = _install_fake_bus(monkeypatch, read_comm_fail={diagnose_servos.ADDR_MAX_POSITION_LIMIT})

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    # Falling back to the safe range would be WIDER than this servo's real
    # limits, so every goal would be rejected while the run reported success.
    assert bus.written_addresses() == []
    assert "Max_Position_Limit" in capsys.readouterr().out


def test_oscillate_servo_reports_a_write_the_servo_rejects(monkeypatch: Any, capsys: Any) -> None:
    bus = _install_fake_bus(monkeypatch, write_error={diagnose_servos.ADDR_GOAL_POSITION: 1})

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    # comm succeeds and only the status packet's error byte says no.
    out = capsys.readouterr().out
    assert "rejected" in out
    assert bus.writes(diagnose_servos.ADDR_TORQUE_ENABLE) == []


def test_oscillate_servo_reports_failure_when_the_swing_breaks_off_early(monkeypatch: Any, capsys: Any) -> None:
    # Goal_Position write 1 is the seed, write 2 is the first swing target.
    bus = _install_fake_bus(monkeypatch, write_error={diagnose_servos.ADDR_GOAL_POSITION: 2})

    # The joint never swung. Returning True prints "Test completed
    # successfully!" and exits 0 over a servo that refused to move.
    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    assert "rejected" in capsys.readouterr().out
    # The exit path still runs: parked, released, profile put back.
    assert bus.writes(diagnose_servos.ADDR_TORQUE_ENABLE)[-1] == 0


def test_oscillate_servo_leaves_a_servo_it_never_energized_alone_when_the_bus_dies(monkeypatch: Any) -> None:
    # The cable dies during the read prologue, before any write. The robot may be
    # standing and holding this leg: releasing torque here would drop it.
    bus = _install_fake_bus(
        monkeypatch, {diagnose_servos.ADDR_TORQUE_ENABLE: 1}, read_raises={diagnose_servos.ADDR_DRIVE_MODE}
    )

    with pytest.raises(OSError):
        diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0)

    assert bus.written_addresses() == []
    assert bus.registers[diagnose_servos.ADDR_TORQUE_ENABLE] == 1
    assert bus.closed is True


def test_oscillate_servo_explains_every_prologue_read_it_refuses_on(monkeypatch: Any, capsys: Any) -> None:
    # A bare "Failed to read X" leaves the operator with no idea whether the tool
    # wrote anything before giving up.
    for address, register in (
        (diagnose_servos.ADDR_TORQUE_ENABLE, "Torque_Enable"),
        (diagnose_servos.ADDR_OPERATING_MODE, "Operating_Mode"),
        (diagnose_servos.ADDR_DRIVE_MODE, "Drive_Mode"),
        (diagnose_servos.ADDR_PRESENT_POSITION, "Present_Position"),
        (diagnose_servos.ADDR_MIN_POSITION_LIMIT, "Min_Position_Limit"),
    ):
        bus = _install_fake_bus(monkeypatch, read_comm_fail={address})

        assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

        out = capsys.readouterr().out
        assert register in out
        assert "untouched" in out or "nothing was written" in out
        assert bus.written_addresses() == []


def test_oscillate_servo_does_not_warn_about_a_profile_when_only_the_mode_writes_ran(
    monkeypatch: Any, capsys: Any
) -> None:
    # Not in position control, its profile unreadable, and the torque release the
    # EEPROM write needs is refused: the run stops before any profile write, so
    # there is nothing on the servo to power-cycle away.
    _install_fake_bus(
        monkeypatch,
        {diagnose_servos.ADDR_OPERATING_MODE: 1},
        read_comm_fail={diagnose_servos.ADDR_PROFILE_VELOCITY},
        write_error={diagnose_servos.ADDR_TORQUE_ENABLE: 1},
    )

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    out = capsys.readouterr().out
    assert "still in place" not in out
    assert "Power-cycle" not in out


def test_oscillate_servo_refuses_a_servo_whose_hardware_error_has_latched(monkeypatch: Any, capsys: Any) -> None:
    # Bit 7 of the error byte is set on every packet a servo sends while its
    # Hardware_Error_Status is non-zero. The servo still answers and still obeys.
    bus = _install_fake_bus(monkeypatch, latched=True)

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    out = capsys.readouterr().out
    # Named as the standing condition it is, with the subcommand that clears it.
    assert "latched hardware error" in out
    assert "recover" in out
    # Not as a dropped read of a servo that in fact answered every time.
    assert "Failed to read" not in out
    assert bus.reads(diagnose_servos.ADDR_TORQUE_ENABLE) == 1
    # Detected before the first write, so the servo is left as it was found.
    assert bus.written_addresses() == []


def test_oscillate_servo_reports_a_mid_swing_latch_as_hardware_error_not_a_rejected_write(
    monkeypatch: Any, capsys: Any
) -> None:
    # Goal_Position write 2 is the first swing target: the servo latches Overload
    # while carrying it out, then alerts on every packet after it.
    bus = _install_fake_bus(monkeypatch, latch_on_write=(diagnose_servos.ADDR_GOAL_POSITION, 2))

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    out = capsys.readouterr().out
    # Every write after the latch really was carried out, so none of them may be
    # reported as refused -- least of all the torque release, which the operator
    # reads before letting go of the robot.
    assert "rejected" not in out
    assert "Failed to" not in out
    assert "Torque disabled." in out
    assert bus.registers[diagnose_servos.ADDR_TORQUE_ENABLE] == 0
    # The standing condition is reported once, not once per packet.
    assert out.count("latched hardware error") == 1
    # The profile is still put back: those writes are obeyed too.
    assert bus.registers[diagnose_servos.ADDR_PROFILE_VELOCITY] == 0


def test_oscillate_servo_does_not_warn_about_a_profile_it_never_wrote(monkeypatch: Any, capsys: Any) -> None:
    # A refusal decided from reads alone: nothing was written, so there is no
    # profile in place and no reason to send the operator to a power cycle.
    _install_fake_bus(monkeypatch, read_comm_fail={diagnose_servos.ADDR_DRIVE_MODE})

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    out = capsys.readouterr().out
    assert "still in place" not in out
    assert "Power-cycle" not in out


def test_oscillate_servo_refuses_limits_that_do_not_overlap_the_safe_range(monkeypatch: Any, capsys: Any) -> None:
    # Limits below the safe range: min < max, so the servo's own registers look
    # sane, but narrowing them to [200, 3900] leaves an inverted window.
    bus = _install_fake_bus(
        monkeypatch,
        {
            diagnose_servos.ADDR_MIN_POSITION_LIMIT: 50,
            diagnose_servos.ADDR_MAX_POSITION_LIMIT: 100,
            diagnose_servos.ADDR_PRESENT_POSITION: 80,
        },
    )

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    out = capsys.readouterr().out
    # The operator is shown the servo's real limits, not the [200, 100] that
    # clamping produced and that no register ever reported.
    assert "[50, 100]" in out
    assert "[200, 100]" not in out
    assert bus.written_addresses() == []


def test_oscillate_output_is_ascii_so_any_console_encoding_can_print_it(monkeypatch: Any, capsys: Any) -> None:
    # A console whose encoding cannot carry a character makes `print` itself
    # raise, which on the error paths is what would skip the torque release.
    _install_fake_bus(monkeypatch, {diagnose_servos.ADDR_TORQUE_ENABLE: 1})

    diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0)

    assert capsys.readouterr().out.isascii()


def test_oscillate_servo_warns_when_the_profile_it_would_restore_is_unreadable(monkeypatch: Any, capsys: Any) -> None:
    _install_fake_bus(monkeypatch, read_comm_fail={diagnose_servos.ADDR_PROFILE_VELOCITY})

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    # Profile_Acceleration left behind outlives the script: the SDK rewrites only
    # Profile_Velocity at connect, so the leg would move unlike its peers.
    out = capsys.readouterr().out
    assert "could not be read" in out
    assert "still in place" in out


def test_oscillate_servo_reports_a_failed_profile_restore(monkeypatch: Any, capsys: Any) -> None:
    # The second Profile_Velocity write is the restore in the finally block.
    _install_fake_bus(monkeypatch, write_error={diagnose_servos.ADDR_PROFILE_VELOCITY: 2})

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    assert "restore Profile_Velocity" in capsys.readouterr().out


def test_oscillate_servo_releases_torque_and_closes_the_port_when_the_bus_dies(monkeypatch: Any) -> None:
    # Goal_Position write 1 is the seed, 2 is the first swing: the cable dies mid-swing.
    bus = _install_fake_bus(monkeypatch, write_raises={diagnose_servos.ADDR_GOAL_POSITION: 2})

    with pytest.raises(OSError):
        diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0)

    assert bus.closed is True
    assert bus.writes(diagnose_servos.ADDR_TORQUE_ENABLE)[-1] == 0


def test_oscillate_servo_releases_torque_before_announcing_that_it_did(monkeypatch: Any) -> None:
    # Goal_Position write 2 is the first swing: the cable dies mid-swing, and
    # the announcement of the release cannot be printed (a closed pipe here,
    # an unencodable character on a cp932 console). Printing first would make
    # that the reason the joint stays powered.
    bus = _install_fake_bus(monkeypatch, write_raises={diagnose_servos.ADDR_GOAL_POSITION: 2})
    real_print = print

    def exploding_print(*args: Any, **kwargs: Any) -> None:
        if "Unexpected failure" in " ".join(str(arg) for arg in args):
            raise BrokenPipeError("stdout is gone")
        real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", exploding_print)

    with pytest.raises(OSError):
        diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0)

    assert bus.writes(diagnose_servos.ADDR_TORQUE_ENABLE)[-1] == 0
    assert bus.registers[diagnose_servos.ADDR_TORQUE_ENABLE] == 0


def test_oscillate_servo_closes_the_port_even_when_the_profile_restore_raises(monkeypatch: Any, capsys: Any) -> None:
    # Profile_Acceleration write 1 sets the profile, 2 is the restore in `finally`.
    bus = _install_fake_bus(monkeypatch, write_raises={diagnose_servos.ADDR_PROFILE_ACCELERATION: 2})

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    assert bus.closed is True
    assert "could not restore" in capsys.readouterr().out


def test_oscillate_servo_disables_torque_when_interrupted_mid_swing(monkeypatch: Any) -> None:
    # Ctrl+C lands on the first swing's Goal_Position write (write 1 is the seed).
    bus = _install_fake_bus(
        monkeypatch,
        write_raises={diagnose_servos.ADDR_GOAL_POSITION: 2},
        raises_type=KeyboardInterrupt,
    )

    # An interrupted run never swung: reporting success would print "Test
    # completed successfully!" and exit 0 over a check that did not happen.
    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    assert bus.closed is True
    assert bus.writes(diagnose_servos.ADDR_TORQUE_ENABLE)[-1] == 0
    # The profile is put back even on the interrupt path.
    assert bus.registers[diagnose_servos.ADDR_PROFILE_VELOCITY] == 0


def test_oscillate_servo_disables_torque_when_a_second_interrupt_lands_during_the_return(monkeypatch: Any) -> None:
    # First Ctrl+C on the first swing's goal write, second during the dwell the
    # interrupt handler waits out while the joint returns to its start.
    bus = _install_fake_bus(
        monkeypatch,
        write_raises={diagnose_servos.ADDR_GOAL_POSITION: 2},
        raises_type=KeyboardInterrupt,
        sleep_raises=1,
    )

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    # A KeyboardInterrupt escaping the handler's inner block would skip the
    # release and leave the joint stiff on a robot the operator is holding.
    assert bus.writes(diagnose_servos.ADDR_TORQUE_ENABLE)[-1] == 0
    assert bus.registers[diagnose_servos.ADDR_TORQUE_ENABLE] == 0
    assert bus.closed is True


def test_oscillate_servo_reads_the_present_position_after_the_dwell(monkeypatch: Any) -> None:
    bus = _install_fake_bus(monkeypatch)

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    # Reading before the dwell reports where the joint was one swing ago, which
    # is exactly the observation the tool exists to give.
    events = [(kind, addr) for kind, addr, _ in bus.log]
    goal_writes = [
        i for i, (kind, addr) in enumerate(events) if kind == "write" and addr == diagnose_servos.ADDR_GOAL_POSITION
    ]
    tail = events[goal_writes[1] + 1 :]  # goal_writes[0] is the seed
    first_present = next(
        i for i, (kind, addr) in enumerate(tail) if kind == "read" and addr == diagnose_servos.ADDR_PRESENT_POSITION
    )
    first_sleep = next(i for i, (kind, _) in enumerate(tail) if kind == "sleep")
    assert first_sleep < first_present


def test_oscillate_servo_warns_but_keeps_torque_when_the_mode_is_already_position(
    monkeypatch: Any, capsys: Any
) -> None:
    bus = _install_fake_bus(monkeypatch, {diagnose_servos.ADDR_TORQUE_ENABLE: 1})

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    # No Operating_Mode write is needed, so the joint is never limped before its
    # position is read: torque goes on, then off at the end. Never off first.
    assert bus.writes(diagnose_servos.ADDR_TORQUE_ENABLE) == [1, 0]
    assert "holding torque" in capsys.readouterr().out


def test_oscillate_servo_drops_torque_only_to_write_the_operating_mode(monkeypatch: Any, capsys: Any) -> None:
    bus = _install_fake_bus(
        monkeypatch,
        {diagnose_servos.ADDR_TORQUE_ENABLE: 1, diagnose_servos.ADDR_OPERATING_MODE: 1},
    )

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is True

    assert bus.writes(diagnose_servos.ADDR_TORQUE_ENABLE) == [0, 1, 0]
    assert bus.writes(diagnose_servos.ADDR_OPERATING_MODE) == [diagnose_servos.OPERATING_MODE_POSITION]
    # The release is imminent and nothing waits for the operator, so the warning
    # must not read like a prompt to act between two lines of output.
    assert "released now, without a pause" in capsys.readouterr().out


def test_oscillate_servo_decodes_a_negative_present_position(monkeypatch: Any, capsys: Any) -> None:
    # Present_Position is signed: with a Homing_Offset a joint just below zero
    # reports 0xFFFFFFF6, which read unsigned is ~4.29e9.
    bus = _install_fake_bus(monkeypatch, {diagnose_servos.ADDR_PRESENT_POSITION: 0xFFFFFFF6})

    assert diagnose_servos.oscillate_servo(2, "test-port", 1_000_000, 2.0) is False

    assert bus.written_addresses() == []
    out = capsys.readouterr().out
    assert "sits at -10" in out
    # A joint outside its limits has negative reachable travel; the message must
    # not offer the reader "only -863 units of travel".
    assert "outside the usable range" in out
    assert "units of travel" not in out


def test_oscillate_rejects_an_id_the_robot_does_not_have() -> None:
    parser = diagnose_servos._build_parser()

    # 254 is the Dynamixel broadcast ID: it would address all 21 servos at once.
    with pytest.raises(SystemExit):
        parser.parse_args(["oscillate", "--id", "254"])
    with pytest.raises(SystemExit):
        parser.parse_args(["oscillate", "--id", "0"])
    assert parser.parse_args(["oscillate", "--id", "21"]).id == 21


def test_oscillate_rejects_a_duration_that_would_still_swing() -> None:
    parser = diagnose_servos._build_parser()

    # The swing count floors at two half-swings, so `--duration 0` moves the
    # joint for ~2 s after the user asked for no motion at all.
    for bad in ("0", "-3", "nan", "inf"):
        with pytest.raises(SystemExit):
            parser.parse_args(["oscillate", "--id", "4", "--duration", bad])
    assert parser.parse_args(["oscillate", "--id", "4", "--duration", "8"]).duration == 8.0


def test_diagnose_servos_help_is_ascii(capsys: Any) -> None:
    # --help is printed too, on the same console that cannot encode every
    # character. The top level carries each subcommand's one-line help, the
    # subparser its own options, so neither alone covers the text.
    parser = diagnose_servos._build_parser()

    for argv in (["--help"], ["oscillate", "--help"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
        assert capsys.readouterr().out.isascii()


def test_joints_reports_present_position_without_writing(monkeypatch: Any, capsys: Any) -> None:
    bus = _install_fake_bus(monkeypatch)

    args = argparse.Namespace(port="test-port", baudrate=1_000_000)
    assert diagnose_servos._cmd_joints(args) == 0

    assert bus.written_addresses() == []
    out = capsys.readouterr().out
    assert "pos=1391" in out
    assert "reverse,time-based" in out
    assert "temp=34" in out
    assert out.isascii()


def test_joints_reports_the_position_p_gain(monkeypatch: Any, capsys: Any) -> None:
    # `joints` is the tool for "why did nothing happen", and Position_P_Gain is
    # the register that decides whether a goal is driven at all: a neck left on
    # 6 by the SDK's shutdown park accepts every goal and follows none. Without
    # it in the report there is nothing to compare a stuck joint against.
    bus = _install_fake_bus(monkeypatch, {diagnose_servos.ADDR_POSITION_P_GAIN: _SOFT_RELEASED_NECK_GAIN})

    args = argparse.Namespace(port="test-port", baudrate=1_000_000)
    assert diagnose_servos._cmd_joints(args) == 0

    assert bus.written_addresses() == []
    out = capsys.readouterr().out
    assert "pgain=6" in out
    assert out.isascii()


def test_joints_reports_a_negative_present_position_as_signed(monkeypatch: Any, capsys: Any) -> None:
    _install_fake_bus(monkeypatch, {diagnose_servos.ADDR_PRESENT_POSITION: 0xFFFFFFF6})

    args = argparse.Namespace(port="test-port", baudrate=1_000_000)
    assert diagnose_servos._cmd_joints(args) == 0

    out = capsys.readouterr().out
    assert "pos= -10" in out
    assert "4294967286" not in out


class _Stream:
    """A `sys.stderr` stand-in whose interactivity the test decides."""

    def __init__(self, interactive: bool) -> None:
        self._interactive = interactive

    def isatty(self) -> bool:
        return self._interactive


def _scan_recording_progress_kwargs(monkeypatch: Any, *, stderr_is_a_tty: bool) -> dict[str, Any]:
    """Run one scan against fake hardware; return the keyword arguments tqdm was given."""
    recorded: dict[str, Any] = {}

    class _PortHandler:
        def __init__(self, port: str) -> None:
            pass

        def openPort(self) -> bool:  # noqa: N802 - mirrors the vendor API
            return True

        def setBaudRate(self, baudrate: int) -> bool:  # noqa: N802 - mirrors the vendor API
            return True

        def closePort(self) -> None:  # noqa: N802 - mirrors the vendor API
            pass

    class _PacketHandler:
        def __init__(self, protocol: float) -> None:
            pass

        def ping(self, port_handler: Any, dxl_id: int) -> tuple[int, int, int]:
            return 1000 + dxl_id, 1, 0  # nothing answers; the scan result is not what we assert on

    def _tqdm(values: range, **kwargs: Any) -> _Progress:
        recorded.update(kwargs)
        return _Progress(values)

    monkeypatch.setattr(diagnose_servos, "PortHandler", _PortHandler)
    monkeypatch.setattr(diagnose_servos, "PacketHandler", _PacketHandler)
    monkeypatch.setattr(diagnose_servos, "tqdm", _tqdm)
    monkeypatch.setattr(diagnose_servos.sys, "stderr", _Stream(stderr_is_a_tty))

    diagnose_servos.scan_dynamixel("test-port", 1_000_000, 1, 3)
    return recorded


def test_scan_dynamixel_disables_progress_when_stderr_is_not_a_tty(monkeypatch: Any) -> None:
    # Redirected or piped: the redraws become one line each, 70+ per scan.
    assert _scan_recording_progress_kwargs(monkeypatch, stderr_is_a_tty=False)["disable"] is True


def test_scan_dynamixel_shows_progress_when_stderr_is_a_tty(monkeypatch: Any) -> None:
    assert _scan_recording_progress_kwargs(monkeypatch, stderr_is_a_tty=True)["disable"] is False
