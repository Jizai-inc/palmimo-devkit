# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""ServoDriver — the I/O boundary of the SDK.

The facade (:class:`~palmimo_sdk.robot.Palmimo`) depends only on this abstraction.
Concrete drivers adapt the engine's raw-tick position dict to a specific
backend (serial bus, simulator, ...). :class:`~palmimo_sdk.io.dynamixel.DynamixelDriver`
is the bundled hardware backend; tests use an in-memory fake that implements
this contract.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServoTelemetry:
    """One sweep of the servo health signals, as read from the bus.

    Units differ per signal on purpose. Current stays in the servo's own raw
    register units because a current threshold is only ever trustworthy as a
    measured number, and it is measured in raw units; converting would cut the
    value loose from the measurement it came from. Voltage and temperature are
    physical units because their limits come from the datasheet (the servo's
    minimum input voltage, its rated temperature) and are compared against it.

    A motor missing from a mapping was not read this sweep — it is unknown, not
    zero. Callers must not read an absent motor as healthy.

    The mappings are typed read-only: a sweep is a record of what the servos
    said, and a caller that edits it is editing evidence.

    Attributes:
        current (Mapping[str, int]): Motor name -> present current, raw signed units.
        voltage (Mapping[str, float]): Motor name -> present input voltage, in volts.
        temperature (Mapping[str, int]): Motor name -> present temperature, in °C.
        silent (tuple[str, ...]): Motors that were asked and did not answer.
        unreached (tuple[str, ...]): Motors the sweep stopped short of, which is
            evidence about the sweep rather than about those motors.
    """

    current: Mapping[str, int] = field(default_factory=dict)
    voltage: Mapping[str, float] = field(default_factory=dict)
    temperature: Mapping[str, int] = field(default_factory=dict)
    silent: tuple[str, ...] = ()
    unreached: tuple[str, ...] = ()


logger = logging.getLogger(__name__)


# Safe servo tick range (AGENTS.md "Hardware Safety"): stays clear of the
# mechanical limits at 0/4095 so a bad goal can't stall a joint against them.
# This is a coarse, last-gate guard -- a servo's own Min/Max_Position_Limit is
# usually tighter and wins where it is set.
SAFE_MIN_TICK = 200
SAFE_MAX_TICK = 3900


def clamp_goal_tick(motor: str, tick: int) -> int:
    """Clamp a goal tick into the safe range, warning on each out-of-range write.

    The warning keeps the clamp from silently masking a caller bug; a goal
    already in range logs nothing.

    Args:
        motor (str): Motor name, used only in the warning message.
        tick (int): Raw Dynamixel goal tick to clamp.

    Returns:
        int: *tick*, clamped into ``[SAFE_MIN_TICK, SAFE_MAX_TICK]``.
    """
    clamped = max(SAFE_MIN_TICK, min(SAFE_MAX_TICK, tick))
    if clamped != tick:
        logger.warning(
            "goal %d for %r out of safe range [%d, %d]; clamped to %d",
            tick,
            motor,
            SAFE_MIN_TICK,
            SAFE_MAX_TICK,
            clamped,
        )
    # Compared before the cast: a float already in range is not out of range.
    return int(clamped)


class ServoDriver(ABC):
    """Abstract servo backend.

    Position dicts use the same keys the engine emits — motor name to raw
    Dynamixel tick (0-4095, center 2048), e.g. ``leg_1_yaw``, ``neck_pitch1``.
    """

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Whether the driver currently holds an open connection."""

    @abstractmethod
    def connect(self) -> None:
        """Open the connection and prepare the servos to accept commands."""

    @abstractmethod
    def disconnect(self) -> None:
        """Release the connection."""

    def write_positions(self, positions: dict[str, int]) -> None:
        """Command servo goal positions.

        Every goal is clamped into ``[SAFE_MIN_TICK, SAFE_MAX_TICK]`` here,
        before it reaches :meth:`_write_positions` — a backend never has to
        remember to apply the safe range itself.

        Args:
            positions (dict[str, int]): Motor name -> raw Dynamixel tick.
        """
        self._write_positions({motor: clamp_goal_tick(motor, tick) for motor, tick in positions.items()})

    @abstractmethod
    def _write_positions(self, positions: dict[str, int]) -> None:
        """Send already-clamped servo goal positions to the backend.

        Args:
            positions (dict[str, int]): Motor name -> raw Dynamixel tick,
                already clamped into the safe range by :meth:`write_positions`.
        """

    def read_positions(self) -> dict[str, int]:
        """Read present servo positions (motor name -> raw tick).

        Optional capability. The default raises :class:`NotImplementedError`;
        backends that can sense position (e.g. a serial bus) override it.
        Callers that need it (e.g. timed return-to-neutral) should degrade
        gracefully or surface a clear error when it is unavailable.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support read_positions().")

    def read_telemetry(self, motors: Sequence[str] | None = None) -> ServoTelemetry:
        """Read the servo health signals (current, voltage, temperature) in one sweep.

        One sweep rather than one call per signal: the safety layer reads inside
        the control loop, where three separate reads would not fit in a frame.

        Args:
            motors (Sequence[str], optional): Motors to sweep; ``None`` sweeps
                every motor. A caller that has set a motor aside as unreadable
                passes the rest, and re-checks it later on its own schedule.

        Optional capability; the default raises :class:`NotImplementedError`.
        A backend that cannot sense these leaves the safety guards with nothing
        to judge on, so callers must treat it as "unable to watch" rather than
        as "nothing wrong".
        """
        raise NotImplementedError(f"{type(self).__name__} does not support read_telemetry().")

    def set_profile_velocity(self, ticks_per_second: int | dict[str, int]) -> None:
        """Set the goal-approach speed, in **ticks per second**.

        A scalar applies to every motor; a dict sets per-motor speeds. ``0``
        means "as fast as the backend allows". The unit is deliberately
        hardware-neutral (ticks/s, matching the tick positions the engine
        emits) — concrete drivers convert it to their native register units.

        Optional capability; the default raises :class:`NotImplementedError`.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support set_profile_velocity().")

    def set_position_p_gain(self, value: int | None, motors: list[str] | None = None) -> None:
        """Set Position_P_Gain (servo stiffness).

        ``value`` ``None`` resets to the backend's captured defaults. ``motors``
        writes only the named subset (e.g. the neck soft-release on shutdown);
        ``None`` writes every motor.

        Optional capability; the default raises :class:`NotImplementedError`.
        Callers (e.g. gentle wake / neck park) should degrade gracefully when a
        backend can't ramp gain.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support set_position_p_gain().")
