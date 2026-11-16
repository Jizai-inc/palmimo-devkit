# SPDX-License-Identifier: Apache-2.0
"""Re-arming a servo that answers again after going silent.

A servo that loses power for a moment reboots with its RAM settings back at
their power-on defaults: torque off, default gain and profile. It answers the
bus again, so the guards watch it, but it no longer holds its joint. Re-arming
writes back the gain the robot last gave it, then arms it as connect does: the
goal is set to where the joint is, so torque-on holds it there instead of
jumping to a stale goal, then the profile, then torque.

Every write is idempotent, so a servo that never rebooted is left as it was.
A servo with a hardware error latched is not re-armed: it turned its own torque
off to protect itself, and clearing that takes a reboot, which is the
operator's call.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from ._guard import TripLog
from .hardware_error import describe_hardware_error
from .io.base import ServoDriver


logger = logging.getLogger(__name__)

# Re-arm attempts kept for draining, like the trip queues.
REARM_HISTORY = 64


@dataclass(frozen=True)
class ReArm:
    """One attempt to re-arm a servo that answered again after going silent.

    Attributes:
        motor (str): The servo's name.
        armed (bool): ``True`` when torque was turned back on.
        reason (str): Why it was not re-armed; empty when it was.
        at (float): The robot's monotonic clock reading at the attempt.
    """

    motor: str
    armed: bool
    reason: str
    at: float


class Rearmer:
    """Re-arms servos that answered again, and remembers the ones it could not."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.log: TripLog[ReArm] = TripLog(REARM_HISTORY)
        self._unarmed: dict[str, str] = {}

    @property
    def unarmed(self) -> dict[str, str]:
        """Servos that answered again but were not re-armed, mapped to why."""
        return dict(self._unarmed)

    def reset(self) -> None:
        """Forget the unarmed servos; connect arms every motor."""
        self._unarmed = {}

    def rearm(
        self,
        driver: ServoDriver,
        motors: Iterable[str],
        *,
        excluded: Mapping[str, str],
        restore_gain: Callable[[str], None],
    ) -> bool:
        """Re-arm each of *motors* not in *excluded*, recording every attempt.

        Args:
            driver (ServoDriver): Connected driver.
            motors (Iterable[str]): Servos that answered again.
            excluded (Mapping[str, str]): Servos to leave as they are, mapped to why.
            restore_gain (Callable[[str], None]): Writes a servo's current gain back.

        Returns:
            bool: ``True`` when any servo was re-armed.
        """
        armed = False
        for motor in motors:
            reason = excluded.get(motor)
            if reason is None:
                try:
                    reason = _rearm_motor(driver, motor, restore_gain)
                except NotImplementedError as exc:
                    reason = f"the driver cannot re-arm it ({exc})"
                except Exception as exc:
                    reason = f"re-arming failed ({exc})"
            self.log.record(ReArm(motor, reason is None, reason or "", self._clock()))
            if reason is None:
                armed = True
                self._unarmed.pop(motor, None)
                logger.warning("%s answered again; re-armed it (gain, hold goal, profile, torque on).", motor)
            else:
                self._unarmed[motor] = reason
                logger.error("%s answered again but was not re-armed: %s.", motor, reason)
        return armed


def _rearm_motor(driver: ServoDriver, motor: str, restore_gain: Callable[[str], None]) -> str | None:
    """Arm *motor* unless a hardware error is latched; return why not, or ``None`` once its torque is on.

    Transport failures propagate; the servo may then be partly re-armed, never
    with torque turned on.
    """
    errors = driver.read_hardware_errors([motor])
    bits = errors.errors.get(motor)
    if bits is None:
        return "it did not answer the hardware-error read"
    if bits:
        return f"a hardware error is latched ({describe_hardware_error(bits)}); reboot or power-cycle it"
    restore_gain(motor)
    driver.arm([motor])
    return None
