# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Scheduled reads of ``ServoDriver.read_telemetry()`` for the safety guards.

Each guard watches a fixed set of motors at its own rate, and each has to cope
with the same three outcomes of a read: a sweep, a driver that can never give
one, and a failure that may pass. :class:`TelemetryPoller` owns that part, so a
guard is left with judging the numbers.

The bus reader stops at the first motor that does not answer, so one dead motor
would blind the guard to every motor behind it, on every sweep. The poller sets
such a motor aside and re-checks it on its own schedule.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._guard import TripLog


if TYPE_CHECKING:
    from .io.base import ServoDriver, ServoTelemetry

# How often a failing read is allowed to re-log, so a persistently flaky bus
# does not spam the log once per poll.
READ_FAILURE_LOG_INTERVAL_S = 5.0
# Sweeps in a row a motor may stay silent before it is left out of the sweep.
SILENT_SWEEPS_TO_SET_ASIDE = 3
# Time between two re-checks of the motors that were set aside (one motor each).
RECHECK_INTERVAL_S = 1.5
# Motors set aside at which a guard is as good as blind (capped by how many it watches).
BLIND_SET_ASIDE_COUNT = 3
# Reads in a row that fail or return no motor at all before the guard is reported blind.
EMPTY_READS_TO_BLIND = 3
# How often one motor's set-aside / watched-again changes may re-log, so a flapping servo does not spam.
WATCH_CHANGE_LOG_INTERVAL_S = 30.0
_WATCH_CHANGE_HISTORY = 64


@dataclass(frozen=True)
class WatchChange:
    """One motor leaving or rejoining a guard's telemetry sweep.

    Attributes:
        motor (str): The motor concerned.
        guard (str): The guard that watches it (``"leg telemetry guard"``).
        watched (bool): ``False`` when the motor was set aside as unresponsive,
            ``True`` when it answered a re-check and is watched again.
        at (float): The guard's clock reading when it happened.
    """

    motor: str
    guard: str
    watched: bool
    at: float


class TelemetryPoller:
    """Reads one fixed set of motors at most once per interval.

    The clock stays with the caller, which passes *now* in: a guard already
    needs the same timestamp for its own bookkeeping.

    A motor that stays silent for ``SILENT_SWEEPS_TO_SET_ASIDE`` sweeps in a row
    is not asked for again, and so is absent from every sweep returned, which
    keeps the contract that a missing motor is unknown. Every
    ``RECHECK_INTERVAL_S`` one set-aside motor is added to a sweep to see if it
    answers, and after an answer the next one is tried on the next sweep, so a
    bus that comes back as a whole is watched again within a sweep per motor;
    the reader follows bus order, so a re-check that stays silent
    leaves the motors behind it unreached for that one sweep. Motors the sweep
    never reached are no evidence and are not counted. Nothing here actuates:
    the changes are recorded in :attr:`watch_changes` and logged, at most every
    ``WATCH_CHANGE_LOG_INTERVAL_S`` per motor.

    The guard is logged as blind, once, when ``BLIND_SET_ASIDE_COUNT`` motors
    are set aside or when ``EMPTY_READS_TO_BLIND`` reads in a row fail or carry
    no motor at all (a sweep whose request never went out reaches no one), and
    logged again, once, when it can see.

    Args:
        motors (Sequence[str]): Motors every sweep asks for.
        interval_s (float): Shortest time between two reads.
        guard_name (str): What the log calls the guard that goes blind when
            the driver cannot be read (``"overload guard"``).
        logger (logging.Logger): The guard's own logger, so its records keep
            coming from the guard's module.
    """

    def __init__(
        self,
        motors: Sequence[str],
        interval_s: float,
        *,
        guard_name: str,
        logger: logging.Logger,
    ) -> None:
        self._motors = tuple(motors)
        self._interval_s = interval_s
        self._guard_name = guard_name
        self._logger = logger
        self._last_poll: float | None = None
        self._last_failure_log: float | None = None
        self._disabled = False
        self._silent_counts: dict[str, int] = {}
        self._set_aside: list[str] = []
        self._recheck_due: float | None = None
        self._restored: tuple[str, ...] = ()
        self._empty_reads = 0
        self._blind = False
        self._change_logs: dict[str, tuple[float, int]] = {}
        self.watch_changes: TripLog[WatchChange] = TripLog(_WATCH_CHANGE_HISTORY)

    @property
    def unwatched(self) -> tuple[str, ...]:
        """Motors currently set aside, in the order they are watched."""
        return tuple(m for m in self._motors if m in self._set_aside)

    @property
    def restored(self) -> tuple[str, ...]:
        """Motors that rejoined the sweep on the latest :meth:`read`."""
        return self._restored

    @property
    def disabled(self) -> bool:
        """Whether the driver proved unable to give this sweep at all; cleared by :meth:`reset`."""
        return self._disabled

    def due(self, now: float) -> bool:
        """Whether *interval_s* has passed since the last read."""
        return self._last_poll is None or now - self._last_poll >= self._interval_s

    def read(self, driver: ServoDriver, now: float, failure_message: str) -> ServoTelemetry | None:
        """Read the motors once and return the sweep, or ``None`` if there is none to judge.

        A driver without ``read_telemetry()``, or without every motor asked
        for, cannot change mid-connection: it sets :attr:`disabled` with one
        warning. Any other failure may pass, so it only logs
        *failure_message*, at most every ``READ_FAILURE_LOG_INTERVAL_S``.

        Args:
            driver (ServoDriver): Connected driver to read.
            now (float): The caller's clock reading for this poll.
            failure_message (str): What a transient failure is logged as; the
                exception is appended.
        """
        self._last_poll = now
        self._restored = ()
        probe = self._pick_recheck(now)
        asked = [m for m in self._motors if m not in self._set_aside or m == probe]
        if not asked:
            return None
        try:
            telemetry = driver.read_telemetry(asked)
        except NotImplementedError:
            self._disabled = True
            self._logger.warning(
                "%s does not support read_telemetry(); the %s is disabled.", type(driver).__name__, self._guard_name
            )
        except KeyError as exc:
            self._disabled = True
            self._logger.warning(
                "%s does not carry every motor the %s reads (%s); the %s is disabled.",
                type(driver).__name__,
                self._guard_name,
                exc,
                self._guard_name,
            )
        except Exception as exc:
            if self._last_failure_log is None or now - self._last_failure_log >= READ_FAILURE_LOG_INTERVAL_S:
                self._logger.warning("%s: %s", failure_message, exc)
                self._last_failure_log = now
            self._empty_reads += 1
            self._log_blindness()
        else:
            self._account(telemetry, asked, probe, now)
            return telemetry
        return None

    def _pick_recheck(self, now: float) -> str | None:
        if not self._set_aside:
            return None
        if self._recheck_due is None or now < self._recheck_due:
            return None
        return self._set_aside[0]

    def _account(self, telemetry: ServoTelemetry, asked: Sequence[str], probe: str | None, now: float) -> None:
        answered = {*telemetry.current, *telemetry.voltage, *telemetry.temperature, *telemetry.position}
        # A silent motor is counted by the set-aside path; only a sweep that reached no one is empty.
        if answered:
            self._empty_reads = 0
        elif not telemetry.silent:
            self._empty_reads += 1
        for motor in asked:
            if motor in answered:
                self._silent_counts.pop(motor, None)
                if motor == probe:
                    self._restore(motor, now)
            elif motor in telemetry.silent:
                if motor == probe:
                    self._set_aside.append(self._set_aside.pop(0))
                    self._recheck_due = now + RECHECK_INTERVAL_S
                else:
                    count = self._silent_counts.get(motor, 0) + 1
                    self._silent_counts[motor] = count
                    if count >= SILENT_SWEEPS_TO_SET_ASIDE:
                        self._put_aside(motor, now)
        self._log_blindness()

    def _put_aside(self, motor: str, now: float) -> None:
        self._silent_counts.pop(motor, None)
        self._set_aside.append(motor)
        if self._recheck_due is None:
            self._recheck_due = now + RECHECK_INTERVAL_S
        self.watch_changes.record(WatchChange(motor, self._guard_name, False, now))
        self._log_change(
            motor,
            now,
            f"stayed silent for {SILENT_SWEEPS_TO_SET_ASIDE} sweeps in a row;"
            f" leaving it out of the sweep and re-checking every {RECHECK_INTERVAL_S:.1f}s",
        )

    def _restore(self, motor: str, now: float) -> None:
        self._set_aside.remove(motor)
        # One answer suggests the bus came back as a whole, so the next set-aside motor is tried at once.
        self._recheck_due = now if self._set_aside else None
        self._restored = (*self._restored, motor)
        self.watch_changes.record(WatchChange(motor, self._guard_name, True, now))
        self._log_change(motor, now, "answered again; watching it")

    def _log_change(self, motor: str, now: float, what: str) -> None:
        last, skipped = self._change_logs.get(motor, (None, 0))
        if last is not None and now - last < WATCH_CHANGE_LOG_INTERVAL_S:
            self._change_logs[motor] = (last, skipped + 1)
            return
        self._change_logs[motor] = (now, 0)
        if skipped:
            what += f" ({skipped} earlier changes not logged; see watch_changes)"
        self._logger.warning("%s: %s %s.", self._guard_name, motor, what)

    def _log_blindness(self) -> None:
        empty = self._empty_reads >= EMPTY_READS_TO_BLIND
        blind = empty or len(self._set_aside) >= min(BLIND_SET_ASIDE_COUNT, len(self._motors))
        if blind == self._blind:
            return
        self._blind = blind
        if empty:
            self._logger.error(
                "%s: %d reads in a row returned no motor; the guard can no longer see the bus.",
                self._guard_name,
                self._empty_reads,
            )
        elif blind:
            self._logger.error(
                "%s: %d of %d motors are unresponsive (%s); the guard can no longer see the bus.",
                self._guard_name,
                len(self._set_aside),
                len(self._motors),
                ", ".join(self.unwatched),
            )
        else:
            self._logger.warning("%s: enough motors answer again to watch the bus.", self._guard_name)

    def reset(self, now: float) -> None:
        """Forget the disabled latch, the timers and every set-aside motor, for a newly attached driver.

        :attr:`watch_changes` survives: it is a record, not connection state.
        Each set-aside motor is recorded as watched again, since the next sweep
        asks for it.

        Args:
            now (float): The caller's clock reading, for those records.
        """
        for motor in self.unwatched:
            self.watch_changes.record(WatchChange(motor, self._guard_name, True, now))
        self._disabled = False
        self._last_poll = None
        self._last_failure_log = None
        self._silent_counts.clear()
        self._set_aside.clear()
        self._recheck_due = None
        self._restored = ()
        self._empty_reads = 0
        self._blind = False
        self._change_logs.clear()
