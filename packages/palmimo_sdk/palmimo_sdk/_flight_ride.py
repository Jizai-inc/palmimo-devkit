# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Control-loop wiring; see doc/explanation/servo-flight-recorder.md."""

import logging
from collections.abc import Callable, Mapping

from ._flight_recorder import HARDWARE_ERROR_INTERVAL_S, AxisSample, ServoFlightRecorder
from .engine import Motion
from .io.base import ServoDriver, ServoTelemetry
from .telemetry import READ_FAILURE_LOG_INTERVAL_S


class FlightRide:
    """Reuse guard sweeps and schedule the hardware-error diagnostic read."""

    def __init__(
        self,
        recorder: ServoFlightRecorder,
        *,
        clock: Callable[[], float],
        logger: logging.Logger,
        frame_s: float,
        context_interval_s: float = 1.0,
    ) -> None:
        self._recorder = recorder
        self._clock = clock
        self._logger = logger
        self._frame_s = frame_s
        self._error_interval = HARDWARE_ERROR_INTERVAL_S
        self._context_interval = context_interval_s
        self._disabled = False
        self._last_poll = 0.0
        self._last_context = 0.0
        self._due_frames = 0
        self._last_failure_log: float | None = None
        self._leg: tuple[float, dict[str, AxisSample]] | None = None
        self._neck: tuple[float, dict[str, AxisSample]] | None = None

    @property
    def disabled(self) -> bool:
        return self._disabled

    def on_connect(self, driver: ServoDriver) -> None:
        self._recorder.prepare()
        self._recorder.reset_for_connect()
        self._disabled = False
        self._due_frames = 0
        self._last_failure_log = None
        self._leg = None
        self._neck = None
        now = self._clock()
        self._last_context = now
        for _ in range(3):
            if self._read_errors(driver, self._clock(), "idle") or self._disabled:
                break

    def _read_errors(self, driver: ServoDriver, now: float, motion: str) -> bool:
        self._last_poll = now
        self._due_frames = 0
        try:
            reader = getattr(driver, "read_hardware_errors", None)
            if reader is None:
                raise NotImplementedError("Driver has no hardware-error reader")
            read = reader()
        except (NotImplementedError, KeyError) as exc:
            self._disabled = True
            self._logger.warning("Servo flight recorder disabled: %s", exc)
            return False
        except Exception as exc:
            if self._last_failure_log is None or now - self._last_failure_log >= READ_FAILURE_LOG_INTERVAL_S:
                self._logger.warning("Servo hardware-error read failed: %s", exc)
                self._last_failure_log = now
            return False
        self._recorder.observe_hardware_errors(now, read, motion)
        return not (read.unreached and not read.errors and not read.silent)

    @staticmethod
    def _axes(telemetry: ServoTelemetry, goal: Mapping[str, int]) -> dict[str, AxisSample]:
        names = set(telemetry.current) | set(telemetry.position) | set(telemetry.temperature) | set(telemetry.voltage)
        names.update(telemetry.silent)
        names.update(telemetry.unreached)
        return {
            name: AxisSample(
                telemetry.current.get(name),
                telemetry.position.get(name),
                goal.get(name),
                telemetry.temperature.get(name),
                telemetry.voltage.get(name),
            )
            for name in sorted(names)
        }

    def poll(
        self,
        driver: ServoDriver | None,
        motion: Motion,
        leg_sweep: tuple[float, ServoTelemetry] | None,
        neck_sweep: tuple[float, ServoTelemetry] | None,
        last_goal: Mapping[str, int],
    ) -> bool:
        if driver is None or self._disabled:
            return self._recorder.window_due(self._clock())
        now = self._clock()
        name = motion.name.lower()
        if leg_sweep is not None:
            at, telemetry = leg_sweep
            self._leg = (at, self._axes(telemetry, last_goal))
            self._recorder.capture_fast(at, name, self._leg[1])
        if neck_sweep is not None:
            at, telemetry = neck_sweep
            self._neck = (at, self._axes(telemetry, last_goal))
        if now - self._last_context >= self._context_interval:
            axes = {}
            if self._leg is not None and now - self._leg[0] <= 1.0:
                axes.update(self._leg[1])
            neck_at = None
            if self._neck is not None and now - self._neck[0] <= 1.0:
                neck_at = self._neck[0]
                axes.update(self._neck[1])
            self._neck = None
            self._last_context = now
            self._recorder.capture_context(now, name, axes, self._recorder.incident_servos, neck_at)
        if now - self._last_poll >= self._error_interval:
            if (
                leg_sweep is None
                or self._due_frames >= 2
                or now - self._last_poll >= self._error_interval + 2 * self._frame_s
            ):
                self._read_errors(driver, now, name)
            else:
                self._due_frames += 1
        return self._recorder.window_due(now)
