# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Neck thermal guard — a pure state machine over ``ServoDriver.read_telemetry()``.

The neck holds the head up continuously (a standing-load joint, unlike a leg
that only bears weight during its stance phase), so it earns its own guard.
The XC330's own Temperature Limit control table entry defaults to 70 °C, and
the servo has been observed latching an Overheating HW error (bit 2) and
cutting torque at 71 °C after a sustained holding load (2026-09) — one degree
past that limit, with no software warning beforehand. ``NECK_HOT_C`` sits far
enough below 70 °C that the guard's own action (locking the neck to center)
lands well before the servo's own hard cutoff, instead of racing it.

:class:`NeckThermalGuard` only classifies a temperature reading; it does not
read the driver on its own schedule or act on the robot. Wiring both of those
in is :class:`~palmimo_sdk.robot.Palmimo`'s job (in :meth:`~palmimo_sdk.robot.Palmimo.step`),
which is what keeps this module — and the engine it stays out of — free of I/O.
"""

from __future__ import annotations

import enum
import logging
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from .io.base import ServoDriver

logger = logging.getLogger(__name__)

NECK_MOTORS: tuple[str, ...] = ("neck_pitch1", "neck_pitch2", "neck_yaw")

NECK_WARM_C = 55
NECK_HOT_C = 62
# Equal to NECK_WARM_C on purpose: HOT releases the instant the reading is
# back down to an ordinary WARM level rather than needing a separate, lower
# band -- see NeckThermalGuard._advance_state.
NECK_COOL_C = 55

# Read once a second, not once a (60 fps) frame: a telemetry sweep shares bus
# time with the position writes every frame already needs, and neck
# temperature does not move fast enough for a faster poll to matter.
NECK_POLL_INTERVAL_S = 1.0

# How often a read failure (or a sweep missing one or more neck motors) is
# allowed to re-log, so a persistently flaky bus does not spam the log once
# per poll.
_READ_FAILURE_LOG_INTERVAL_S = 5.0

# How long a judgement is trusted without a fresh, COMPLETE sweep behind it.
# A motor that never comes back (a dead connector, a bus wedge on just that
# ID) must not leave a stale HOT judgement in place forever -- nor a stale,
# falsely reassuring NORMAL -- so the guard gives up and reports UNMONITORED
# once its last complete reading is this old.
NECK_STALE_S = 10.0


class NeckThermalState(enum.Enum):
    """Neck thermal guard state, in escalating order."""

    NORMAL = "normal"
    WARM = "warm"
    HOT = "hot"
    UNMONITORED = "unmonitored"


def _classify(temperature_c: float) -> NeckThermalState:
    """Threshold-only classification, ignoring hysteresis."""
    if temperature_c >= NECK_HOT_C:
        return NeckThermalState.HOT
    if temperature_c >= NECK_WARM_C:
        return NeckThermalState.WARM
    return NeckThermalState.NORMAL


class NeckThermalGuard:
    """Classifies neck servo temperature into a :class:`NeckThermalState`.

    A pure state machine: :meth:`poll` takes a driver (or ``None``) and the
    wall clock is injected via *now* (defaulting to :func:`time.monotonic`),
    so tests drive it without real sleeps. It never touches the engine or the
    driver's write side — the caller reads :attr:`state` / :attr:`temperature_c`
    / :attr:`neck_lock_active` and decides what to do (force ``look()`` to
    center, reject NOD/HEAD_SHAKE).

    State transitions use the worst (highest) reading across the tracked neck
    motors:

    - NORMAL <-> WARM <-> HOT at the ``NECK_WARM_C`` / ``NECK_HOT_C``
      boundaries, escalating immediately.
    - HOT is latched (hysteresis): once entered, :attr:`state` cannot fall
      below HOT until the reading drops to ``NECK_COOL_C`` or below.
    - UNMONITORED when there is no driver, the driver does not implement
      ``read_telemetry()``, no judgement has ever been reached yet (the
      constructed-but-never-polled default), or the last COMPLETE sweep is
      older than ``NECK_STALE_S`` (see :attr:`last_complete_read_at`).

    :attr:`neck_lock_active` tracks the guard's HOT lockout independently of
    :attr:`state` going UNMONITORED: a neck that was HOT and then loses
    telemetry (a motor stops answering) stays locked — reporting UNMONITORED
    while quietly releasing the lockout would let a caller drive an
    unobserved, possibly still-hot neck. The lock only clears on a driver
    that reports ``None`` (nothing left to write to) or a COMPLETE sweep at
    or below ``NECK_COOL_C``.

    A sweep missing one or more of the tracked motors cannot support a fresh
    judgement (an unread motor is unknown, not healthy), so the guard keeps
    its last judgement instead of guessing -- until that judgement goes stale
    (``NECK_STALE_S``), at which point it falls back to UNMONITORED rather
    than trusting a reading that may no longer reflect reality.
    """

    def __init__(
        self,
        motors: Sequence[str] = NECK_MOTORS,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._motors = tuple(motors)
        self._now = now
        self._state = NeckThermalState.UNMONITORED
        self._temperature_c: float | None = None
        self._last_poll: float | None = None
        self._last_failure_log: float | None = None
        self._last_missing_log: float | None = None
        # Monotonic timestamp of the last sweep that read every tracked motor
        # (see NECK_STALE_S / _check_stale). None until the first one lands.
        self._last_complete_read_at: float | None = None
        # Latched once a driver's read_telemetry() proves unsupported (raises
        # NotImplementedError) -- the capability cannot change mid-connection,
        # so re-checking every poll would only repeat the warning below.
        self._unsupported = False
        # See neck_lock_active. Survives the state falling to UNMONITORED
        # (a stale or incomplete sweep); only a driver=None poll or a
        # complete sweep at/below NECK_COOL_C clears it.
        self._neck_lock_active = False

    @property
    def state(self) -> NeckThermalState:
        """The current thermal state."""
        return self._state

    @property
    def temperature_c(self) -> float | None:
        """Highest neck motor temperature from the last successful sweep, or ``None``."""
        return self._temperature_c

    @property
    def neck_lock_active(self) -> bool:
        """Whether the HOT lockout (center-lock, gesture rejection) is currently in force.

        Unlike :attr:`state`, this stays ``True`` through a stale/incomplete
        sweep that follows a HOT judgement — see the class docstring. Callers
        that decide whether to force the neck to center or reject a gesture
        should check this, not ``state == HOT``.
        """
        return self._neck_lock_active

    @property
    def last_complete_read_at(self) -> float | None:
        """Monotonic timestamp (per *now*) of the last sweep that read every tracked motor.

        ``None`` if no complete sweep has ever landed. Callers use this (via
        :attr:`~palmimo_sdk.robot.Palmimo.neck_telemetry_age_s`) to see how
        stale the current judgement is without waiting for it to actually
        flip to UNMONITORED at ``NECK_STALE_S``.
        """
        return self._last_complete_read_at

    def reset(self) -> None:
        """Clear the unsupported-driver latch, poll timer, and log-suppression timers.

        Call after attaching a new driver (e.g. on reconnect) so a guard that
        gave up on a previous, telemetry-less (or persistently flaky) driver
        gets a fresh chance to read the new one, and re-logs promptly if the
        new one has its own problem rather than being silently suppressed by
        a timer left over from the old one.
        """
        self._unsupported = False
        self._last_poll = None
        self._last_failure_log = None
        self._last_missing_log = None

    def poll(self, driver: ServoDriver | None) -> None:
        """Read neck temperature at most once every ``NECK_POLL_INTERVAL_S``, updating :attr:`state`.

        *driver* is ``None`` for compute-only mode or a disconnected driver —
        the guard goes silently UNMONITORED and releases :attr:`neck_lock_active`
        (there is nothing left to write a centering command to). Call once per
        control cycle; the interval throttling lives here, not in the caller,
        so :meth:`~palmimo_sdk.robot.Palmimo.step` doesn't have to track it.
        """
        if driver is None:
            self._state = NeckThermalState.UNMONITORED
            self._temperature_c = None
            self._neck_lock_active = False
            return
        if self._unsupported:
            return
        now = self._now()
        if self._last_poll is not None and now - self._last_poll < NECK_POLL_INTERVAL_S:
            return
        self._last_poll = now
        try:
            telemetry = driver.read_telemetry(self._motors)
        except NotImplementedError:
            self._unsupported = True
            self._state = NeckThermalState.UNMONITORED
            self._temperature_c = None
            self._neck_lock_active = False
            logger.warning(
                "%s does not support read_telemetry(); the neck thermal guard is disabled.",
                type(driver).__name__,
            )
            return
        except Exception as exc:
            if self._last_failure_log is None or now - self._last_failure_log >= _READ_FAILURE_LOG_INTERVAL_S:
                if self._last_complete_read_at is None:
                    logger.warning("neck telemetry read failed before any thermal judgement was reached: %s", exc)
                else:
                    logger.warning("neck telemetry read failed, keeping the last thermal judgement: %s", exc)
                self._last_failure_log = now
            self._check_stale(now)
            return
        readings = {m: telemetry.temperature[m] for m in self._motors if m in telemetry.temperature}
        if len(readings) < len(self._motors):
            missing = [m for m in self._motors if m not in readings]
            if self._last_missing_log is None or now - self._last_missing_log >= _READ_FAILURE_LOG_INTERVAL_S:
                logger.warning(
                    "neck telemetry sweep missing %s; the guard cannot judge from it -- keeping the last "
                    "judgement (and lock, if one is active) until it is %.0fs stale.",
                    missing,
                    NECK_STALE_S,
                )
                self._last_missing_log = now
            self._check_stale(now)
            return  # one or more neck motors unread this sweep -- keep the last judgement
        self._temperature_c = float(max(readings.values()))
        self._last_complete_read_at = now
        self._advance_state(self._temperature_c)

    def _check_stale(self, now: float) -> None:
        """Fall back to UNMONITORED once the last complete sweep is too old to trust.

        Leaves :attr:`neck_lock_active` untouched — a lock earned while HOT
        must survive the guard losing sight of the neck, not release just
        because nobody can currently confirm it has cooled.
        """
        if self._last_complete_read_at is None:
            return  # never had a complete sweep -- state is already UNMONITORED
        if now - self._last_complete_read_at < NECK_STALE_S:
            return
        if self._state is not NeckThermalState.UNMONITORED:
            logger.warning(
                "neck thermal guard: no complete telemetry sweep in over %.0fs -- reporting UNMONITORED (lock %s).",
                NECK_STALE_S,
                "stays engaged" if self._neck_lock_active else "was already released",
            )
        self._state = NeckThermalState.UNMONITORED
        self._temperature_c = None

    def _advance_state(self, temperature_c: float) -> None:
        previous = self._state
        raw = _classify(temperature_c)
        # latched until it cools to NECK_COOL_C or below
        new_state = NeckThermalState.HOT if previous is NeckThermalState.HOT and temperature_c > NECK_COOL_C else raw
        self._state = new_state
        if new_state is NeckThermalState.HOT:
            self._neck_lock_active = True
        elif temperature_c <= NECK_COOL_C:
            self._neck_lock_active = False
        if new_state == previous:
            return
        if new_state is NeckThermalState.HOT:
            logger.error(
                "neck thermal guard: %s -> HOT at %.1f°C; look() forced to center and NOD/HEAD_SHAKE "
                "disabled until the neck cools to %.1f°C.",
                previous.value,
                temperature_c,
                NECK_COOL_C,
            )
        else:
            logger.warning(
                "neck thermal guard: %s -> %s at %.1f°C.",
                previous.value,
                new_state.value,
                temperature_c,
            )
