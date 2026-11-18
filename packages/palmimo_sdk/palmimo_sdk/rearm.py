# SPDX-License-Identifier: Apache-2.0
"""Re-arming a servo that answers again after going silent, or that was rebooted.

A servo that loses power for a moment reboots with its RAM settings back at
their power-on defaults: torque off, default gain and profile. It answers the
bus again, so the guards watch it, but it no longer holds its joint. Re-arming
writes back the gain the robot last gave it, then arms it as connect does: the
goal is set to where the joint is, so torque-on holds it there instead of
jumping to a stale goal, then the profile, then torque.

Every write is idempotent, so a servo that never rebooted is left as it was.
A servo with a hardware error latched is not re-armed: it turned its own torque
off to protect itself, and clearing that takes a reboot, which is the
operator's call. :meth:`Rearmer.reboot` is that reboot: it reboots the servo,
waits for it to answer, and re-arms it the same way.
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
    """One attempt to re-arm a servo that answered again after going silent, or that was rebooted.

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
    """Re-arms servos that answered again or were rebooted, and remembers the ones it could not."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.log: TripLog[ReArm] = TripLog(REARM_HISTORY)
        self._unarmed: dict[str, str] = {}

    @property
    def unarmed(self) -> dict[str, str]:
        """Servos that answered again, or were rebooted, but were not re-armed, mapped to why."""
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
            record = self._attempt(
                motor, excluded, lambda m: _rearm_motor(driver, m, restore_gain), "answered again", "re-arming"
            )
            armed = armed or record.armed
        return armed

    def reboot(
        self,
        driver: ServoDriver,
        motors: Iterable[str],
        *,
        excluded: Mapping[str, str],
        restore_gain: Callable[[str], None],
    ) -> list[ReArm]:
        """Reboot each of *motors* not in *excluded*, then re-arm it, recording every attempt.

        A reboot clears a latched hardware error and leaves the servo limp, so
        each one is re-armed as :meth:`rearm` does. A motor in *excluded* is
        neither rebooted nor armed. One motor's failure does not stop the rest.

        Args:
            driver (ServoDriver): Connected driver.
            motors (Iterable[str]): Servos to reboot.
            excluded (Mapping[str, str]): Servos to leave as they are, mapped to why.
            restore_gain (Callable[[str], None]): Writes a servo's current gain back.

        Returns:
            list[ReArm]: One record per motor, in the order given.
        """

        def reboot_and_arm(motor: str) -> str | None:
            driver.reboot([motor])
            return _rearm_motor(driver, motor, restore_gain)

        return [self._attempt(motor, excluded, reboot_and_arm, "was rebooted", "rebooting") for motor in motors]

    def _attempt(
        self,
        motor: str,
        excluded: Mapping[str, str],
        action: Callable[[str], str | None],
        event: str,
        verb: str,
    ) -> ReArm:
        """Run *action* on *motor* unless excluded, then record and log the outcome."""
        reason = excluded.get(motor)
        if reason is None:
            try:
                reason = action(motor)
            except NotImplementedError as exc:
                reason = f"the driver cannot do it ({exc})"
            except Exception as exc:
                reason = f"{verb} failed ({exc})"
        record = ReArm(motor, reason is None, reason or "", self._clock())
        self.log.record(record)
        if reason is None:
            self._unarmed.pop(motor, None)
            logger.warning("%s %s; re-armed it (gain, hold goal, profile, torque on).", motor, event)
        else:
            self._unarmed[motor] = reason
            logger.error("%s %s but was not re-armed: %s.", motor, event, reason)
        return record


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
