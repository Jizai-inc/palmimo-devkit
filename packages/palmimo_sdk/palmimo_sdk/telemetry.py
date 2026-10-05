# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Scheduled reads of ``ServoDriver.read_telemetry()`` for the safety guards.

Each guard watches a fixed set of motors at its own rate, and each has to cope
with the same three outcomes of a read: a sweep, a driver that can never give
one, and a failure that may pass. :class:`TelemetryPoller` owns that part, so a
guard is left with judging the numbers.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from .io.base import ServoDriver, ServoTelemetry

# How often a failing read is allowed to re-log, so a persistently flaky bus
# does not spam the log once per poll.
READ_FAILURE_LOG_INTERVAL_S = 5.0


class TelemetryPoller:
    """Reads one fixed set of motors at most once per interval.

    The clock stays with the caller, which passes *now* in: a guard already
    needs the same timestamp for its own bookkeeping.

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
        try:
            return driver.read_telemetry(self._motors)
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
        return None

    def reset(self) -> None:
        """Forget the disabled latch and both timers, for a newly attached driver."""
        self._disabled = False
        self._last_poll = None
        self._last_failure_log = None
