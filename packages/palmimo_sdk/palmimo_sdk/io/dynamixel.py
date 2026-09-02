# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""DynamixelDriver — the concrete :class:`ServoDriver` for Palmimo's hardware.

Adapts the engine's raw-tick position dict to a Dynamixel serial bus
(USB-to-servo bridge -> Dynamixel chain) via :class:`~palmimo_sdk.io._dynamixel_bus.DynamixelBus`
(direct ``dynamixel_sdk``). This is the single home for the 21-motor
layout, which :func:`palmimo_motor_ids` exposes on the SDK's public surface.

The serial deps (``dynamixel_sdk``, ``pyserial``) are imported lazily inside the
bus factory, so ``import palmimo_sdk`` stays hardware-free and compute-only use
needs nothing extra.

:func:`find_servo_port` locates the bus's USB bridge and lives here because
the port it finds is this driver's transport — the same finder-with-device
pattern as ``display.py``'s ``find_face_port``. ``DynamixelDriver(port=None)``
calls it at connect time.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Callable, Sequence
from typing import Any

from ..kinematics import NEUTRAL
from ._timeout import ProbeTimeoutError, run_with_timeout
from .base import ServoDriver, ServoTelemetry


logger = logging.getLogger(__name__)


SUPPORTED_MOTOR_MODELS = ("xl330-m288", "xc330-m288")

_TICKS_PER_REV = 4096
# One Profile_Velocity register unit ≈ 0.229 rev/min on the servo's control table.
_DXL_VELOCITY_UNIT_RPM = 0.229
# Profile_Acceleration = velocity / this → ~constant ~0.5s decel ramp, so a glide
# eases to a stop instead of snapping (rectangular profile, acceleration 0).
_ACCEL_RAMP_DIVISOR = 8
# Goal clamp (AGENTS.md "safe servo range"): coarse last-gate guard so a bad goal
# can't stall a joint at 0/4095. Per-joint mechanical limits are tighter.
SAFE_MIN_TICK = 200
SAFE_MAX_TICK = 3900
# The servo's Position_P_Gain power-on default. Re-applied at connect so a prior
# neck soft-release (which lowers the gain in RAM) can't linger on a soft reconnect.
_DEFAULT_POSITION_P_GAIN = 900

# The health signals the safety layer watches, read as one span. Present_Current
# through Present_Temperature is a contiguous range on the control table, so the
# registers in between ride along at no extra cost.
TELEMETRY_REGISTERS = ("Present_Current", "Present_Input_Voltage", "Present_Temperature")
# Present_Input_Voltage counts tenths of a volt.
_VOLTAGE_UNITS_PER_VOLT = 10.0

# Deadline for the whole connect sequence (port detection + bus open + 21-motor
# handshake + arming). Real hardware completes it in well under a second at
# 1 Mbaud; 10s is wide margin for a loaded Pi while still failing fast when no
# robot is attached or the matched port is some other device (see _timeout.py).
_BUS_CONNECT_TIMEOUT_S = 10.0


class DynamixelConnectTimeoutError(RuntimeError):
    """Raised when connecting to the servo bus exceeds :data:`_BUS_CONNECT_TIMEOUT_S`.

    Distinct from :class:`PortDetectionError` (which fires immediately when
    port auto-detection finds zero or multiple candidates): this fires when a
    port WAS resolved but opening it / handshaking with it never completed --
    e.g. no robot attached, or the matched port belongs to a different device.
    """


def palmimo_motor_ids() -> dict[str, int]:
    """Palmimo's 21-motor name -> Dynamixel ID map (the layout's single source).

    IDs 1-18: 6 legs x 3 joints (yaw, pitch1, pitch2 from body to tip).
    IDs 19-21: neck (pitch1, pitch2, yaw from body to head). Names match the
    keys the engine emits (``leg_{1-6}_{yaw,pitch1,pitch2}``, ``neck_*``).
    """
    ids: dict[str, int] = {}
    for leg_id in range(1, 7):
        base_id = (leg_id - 1) * 3 + 1
        ids[f"leg_{leg_id}_yaw"] = base_id
        ids[f"leg_{leg_id}_pitch1"] = base_id + 1
        ids[f"leg_{leg_id}_pitch2"] = base_id + 2
    ids["neck_pitch1"] = 19
    ids["neck_pitch2"] = 20
    ids["neck_yaw"] = 21
    return ids


_SERVO_BUS_VID = 0x2F5D
"""USB vendor ID of the servo bus's USB-to-servo bridge (enumerates as ``2f5d:2202``)."""

_PATTERN_LINUX = re.compile(r"^/dev/ttyACM\d+$")
# macOS has both cu.* (callout) and tty.* (dial-in) nodes per port, but
# serial.tools.list_ports.comports() enumerates only the cu.* node
# (IOCalloutDevice; see pyserial list_ports_osx.py) -- so match cu.*, not tty.*.
# Windows ports surface as COM* and never match these patterns; there the VID
# match above is the only identifier available.
_PATTERN_MACOS = re.compile(r"^/dev/cu\.usbmodem")


class PortDetectionError(RuntimeError):
    """Raised when servo-bus port auto-detection fails.

    The message always includes a remedy (specify ``--port`` explicitly).
    """


def find_servo_port() -> str:
    """Auto-detect the servo bus's serial port (its USB-to-servo bridge).

    Detection priority:

    1. Match on the servo bus's USB vendor ID (:data:`_SERVO_BUS_VID`) — the
       only identifier Windows exposes, where the bridge enumerates as a
       nameless generic "USB Serial Device".
    2. If step 1 finds no candidates, fall back to pattern matching:
       ``/dev/ttyACM<n>`` on Linux, ``/dev/cu.usbmodem*`` on macOS.
    3. Exactly one candidate -> return it. Zero candidates -> error.
       Two or more candidates -> error listing them (refuses to guess).

    Returns:
        str: Device path of the detected port (e.g. ``"/dev/ttyACM0"``).

    Raises:
        PortDetectionError: When zero candidates are found, or when multiple
            candidates exist and it is unsafe to pick one automatically.
    """
    import serial.tools.list_ports  # pyserial; lazy to keep compute-only imports clean

    ports = list(serial.tools.list_ports.comports())

    # Step 1: USB vendor-ID match (Windows: generic name, VID is all we get)
    vid_candidates = [p.device for p in ports if getattr(p, "vid", None) == _SERVO_BUS_VID]
    if vid_candidates:
        return _resolve_single(vid_candidates, "servo bus VID-match candidates")

    # Step 2: pattern fallback (platform path patterns)
    pattern_candidates = [p.device for p in ports if _PATTERN_LINUX.match(p.device) or _PATTERN_MACOS.match(p.device)]
    return _resolve_single(pattern_candidates, "servo bus pattern-match candidates")


def _resolve_single(candidates: list[str], label: str) -> str:
    """Return the sole candidate, or raise :class:`PortDetectionError`.

    Args:
        candidates: List of device paths from a detection pass.
        label: Human-readable label used in the error message.

    Returns:
        str: The single candidate device path.

    Raises:
        PortDetectionError: When *candidates* is empty or has more than one
            entry.
    """
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise PortDetectionError("Servo bus not found. Check connection or specify with --port.")
    listed = ", ".join(candidates)
    raise PortDetectionError(f"Multiple {label} found: {listed}. Cannot safely choose one -- specify with --port.")


def _open_dynamixel_bus(
    port: str,
    motor_model: str,
    baudrate: int,
    profile_velocity: int,
    calibration: dict[str, Any] | None = None,
) -> Any:
    """Build, connect, and arm a :class:`DynamixelBus` ready to take goals.

    The single seam that opens the serial bus; tests inject a fake factory in
    its place so the driver's contract can be exercised without hardware.

    *calibration* is forwarded for API parity and future use; the
    bus operates in raw ticks, so it is not applied — the frame is unchanged.
    """
    from ._dynamixel_bus import OPERATING_MODE_POSITION, DynamixelBus

    bus = DynamixelBus(port=port, motors=palmimo_motor_ids(), model=motor_model, calibration=calibration)
    bus.set_baudrate(baudrate)
    bus.connect()
    with bus.torque_disabled():
        bus.configure_motors()
        for motor in bus.motors:
            bus.write("Operating_Mode", motor, OPERATING_MODE_POSITION)
        # Hold the current pose so re-enabling torque doesn't snap to a stale
        # Goal_Position. Read with num_retry=3 — this bus is flaky and a single
        # read can drop, which would skip the hold and let it snap. Mirrors the
        # current-survey scripts' gentle bring-up (motion_current_survey.py).
        try:
            pres = bus.sync_read("Present_Position", normalize=False, num_retry=3)
            if pres is None:
                # A fully dropped batch read returns None; skip seeding rather than
                # AttributeError into the except below (keeps the warning accurate).
                logger.warning("could not seed hold pose before torque-on: read returned None")
            else:
                for motor in bus.motors:
                    tick = pres.get(motor)
                    if tick is not None:
                        bus.write("Goal_Position", motor, tick, normalize=False)
        except Exception as exc:
            logger.warning("could not seed hold pose before torque-on: %s", exc)
    bus.sync_write("Profile_Velocity", profile_velocity, normalize=False)
    bus.enable_torque()
    return bus


class DynamixelDriver(ServoDriver):
    """Concrete servo backend driving Dynamixels over a serial bus.

    Args:
        port (str | None): Serial port of the servo bus's USB-to-servo bridge (e.g. ``/dev/ttyACM0``). ``None``
            (default) auto-detects it with :func:`find_servo_port` at
            :meth:`connect` time — the same contract as
            :class:`~palmimo_sdk.io.display.FaceDisplay`. Detection failure raises
            :class:`PortDetectionError` from :meth:`connect`.
        baudrate (int): Bus baudrate (default 1_000_000, matching the bridge firmware).
        motor_model (str): Dynamixel model key; ``"xc330-m288"`` is the real hardware (the
            ``"xl330-m288"`` key fails the connect handshake on actual units).
        profile_velocity (int): Time-based Profile_Velocity register value applied to every motor at
            connect time, in milliseconds per move — lower is faster (see the
            :attr:`profile_velocity` property).
        keep_torque_on_disconnect (bool): When ``False`` (default) torque is cut on :meth:`disconnect` so the
            robot relaxes; the facade eases to neutral first in its ``with`` exit.
        calibration (dict | None): Optional per-motor homing offsets, forwarded to the bus
            for API parity and future use but **not applied** — the bus exchanges raw ticks,
            so the coordinate frame is unchanged whether this is ``None`` (default) or a dict.
        connect_timeout (float): Seconds before :meth:`connect` gives up with
            :class:`DynamixelConnectTimeoutError` instead of hanging (default
            :data:`_BUS_CONNECT_TIMEOUT_S`; contract details in :mod:`palmimo_sdk.io._timeout`).
    """

    def __init__(
        self,
        port: str | None = None,
        *,
        baudrate: int = 1_000_000,
        motor_model: str = "xc330-m288",
        profile_velocity: int = 300,
        keep_torque_on_disconnect: bool = False,
        calibration: dict[str, Any] | None = None,
        bus_factory: Callable[..., Any] = _open_dynamixel_bus,
        connect_timeout: float = _BUS_CONNECT_TIMEOUT_S,
    ) -> None:
        if motor_model not in SUPPORTED_MOTOR_MODELS:
            raise ValueError(f"Unsupported motor_model {motor_model!r}; expected one of {SUPPORTED_MOTOR_MODELS}.")
        self._port = port
        self._baudrate = baudrate
        self._motor_model = motor_model
        self._profile_velocity = profile_velocity
        self._keep_torque = keep_torque_on_disconnect
        self._calibration = calibration
        self._bus_factory = bus_factory
        self._connect_timeout = connect_timeout
        self._bus: Any = None
        # Optional command-side IIR low-pass on goal positions (off by default):
        # y[n] = alpha*x[n] + (1-alpha)*y[n-1]. Smooths the stream so a raised
        # P gain tracks crisply without amplifying jitter. _iir_state holds y[n-1].
        self._iir_enabled: bool = False
        self._iir_alpha: float = 1.0
        self._iir_state: dict[str, float] = {}
        self._iir_motors: set[str] | None = None  # None = all motors; else only these
        # Last Position_P_Gain written via set_position_p_gain (None = at default).
        self._p_gain: int | None = None
        # Per-motor Position_P_Gain captured at connect, so set_position_p_gain(None)
        # can restore the firmware defaults after experimenting with higher gains.
        self._default_p_gain: dict[str, int] = {}

    @property
    def is_connected(self) -> bool:
        return self._bus is not None and self._bus.is_connected

    @property
    def profile_velocity(self) -> int:
        """The configured default time-based Profile_Velocity (ms per move).

        The value gesture tuning restores to when it releases a temporary
        per-motion profile — the driver's own setting, not a hardcoded one.
        """
        return self._profile_velocity

    def connect(self) -> None:
        if self.is_connected:
            return

        # Recorded the instant port resolution succeeds, so the timeout error
        # can name the real port even when the hang happened later.
        resolved_port: list[str] = []

        def _open() -> Any:
            # Resolve per connect (not once in __init__) so a device that
            # re-enumerated on a different path is still found on reconnect.
            port = self._port or find_servo_port()
            resolved_port.append(port)
            return self._bus_factory(port, self._motor_model, self._baudrate, self._profile_velocity, self._calibration)

        def _on_late_bus(bus: Any) -> None:
            # Connect outlived its caller -- the %.1fs timeout fired, or a
            # shutdown signal interrupted the wait. Either way nobody owns this
            # armed bus, so close it (cutting torque) instead of leaking it.
            logger.warning(
                "Dynamixel bus connect on %r finished after its caller had given up "
                "(%.1fs timeout, or an interrupted wait); closing the orphaned, armed bus.",
                resolved_port[0] if resolved_port else (self._port or "<auto-detected port>"),
                self._connect_timeout,
            )
            with contextlib.suppress(Exception):
                bus.disconnect(True)

        try:
            self._bus = run_with_timeout(_open, timeout=self._connect_timeout, on_late_result=_on_late_bus)
        except ProbeTimeoutError as exc:
            port_desc = resolved_port[0] if resolved_port else (self._port or "auto-detected servo bus port")
            raise DynamixelConnectTimeoutError(
                f"Timed out connecting to the Dynamixel servo bus on {port_desc!r} after "
                f"{self._connect_timeout:.1f}s (no response during port open / motor handshake / arming). "
                "Check that a robot is attached and powered, and that this is the correct port."
            ) from exc
        # Re-apply the default gain (undo any lingering neck soft-release from a
        # prior session), then snapshot it so set_position_p_gain(None) can reset.
        try:
            self._bus.sync_write("Position_P_Gain", _DEFAULT_POSITION_P_GAIN, normalize=False)
            self._default_p_gain = dict.fromkeys(self._bus.motors, _DEFAULT_POSITION_P_GAIN)
        except Exception as exc:  # non-fatal: reset will just no-op if it fails
            logger.warning("could not apply default Position_P_Gain: %s", exc)
            self._default_p_gain = {}

    def disconnect(self) -> None:
        if self._bus is None:
            return
        # Clear _bus even if the bus disconnect raises, so a half-open driver
        # (e.g. USB yanked) reports disconnected and never wedges as is_connected.
        try:
            self._bus.disconnect(not self._keep_torque)
        finally:
            self._bus = None

    def write_positions(self, positions: dict[str, int]) -> None:
        """Command goal positions, filling un-named motors with neutral.

        The engine emits every motor each frame, but we defensively backfill
        any missing key with ``NEUTRAL`` so a partial dict can never leave a
        joint at a stale goal. Every goal is clamped to the safe tick range as a
        last-gate guard against a bad caller stalling a servo at a hard limit.
        """
        if self._bus is None:
            raise RuntimeError("Driver is not connected. Call connect() before write_positions().")
        # Backfill missing/None with NEUTRAL (no stray None reaches int()), then clamp.
        goal = {
            name: NEUTRAL if (tick := positions.get(name)) is None else self._clamp_goal(name, int(tick))
            for name in self._bus.motors
        }
        if self._iir_enabled:
            goal = self._apply_iir(goal)
        self._bus.sync_write("Goal_Position", goal, normalize=False)

    def _apply_iir(self, goal: dict[str, int]) -> dict[str, int]:
        """First-order IIR low-pass on goal ticks: y = a*x + (1-a)*y_prev.

        Only motors in ``_iir_motors`` are filtered (``None`` = all); others pass
        through untouched, so the filter can target e.g. just the waving arm.
        """
        a = self._iir_alpha
        sel = self._iir_motors
        out: dict[str, int] = {}
        for name, tick in goal.items():
            if sel is not None and name not in sel:
                out[name] = tick
                continue
            prev = self._iir_state.get(name, float(tick))
            y = a * tick + (1.0 - a) * prev
            self._iir_state[name] = y
            out[name] = round(y)
        return out

    def set_iir(self, enabled: bool, alpha: float | None = None, motors: list[str] | None = None) -> None:
        """Enable/disable the command-side IIR low-pass and set its alpha.

        *alpha* in (0, 1]: 1.0 = no smoothing, smaller = heavier smoothing/lag.
        *motors* limits the filter to those motors (e.g. the waving arm); ``None``
        filters every motor. Disabling clears the filter state.
        """
        if alpha is not None:
            self._iir_alpha = max(0.01, min(1.0, float(alpha)))
        self._iir_motors = set(motors) if motors else None
        self._iir_enabled = bool(enabled)
        if not enabled:
            self._iir_state.clear()

    def set_position_p_gain(self, value: int | None, motors: list[str] | None = None) -> None:
        """Write Position_P_Gain (RAM, addr 84); ``None`` = reset to defaults.

        Raising P gain tightens position tracking for the quick wave strokes.
        ``None`` restores the per-motor defaults captured at connect time. Pass
        *motors* to write only a subset (e.g. the neck soft-release on shutdown);
        leaving it ``None`` writes every motor. Writes are RAM-only, so they take
        effect immediately without torque-off.
        """
        if self._bus is None:
            raise RuntimeError("Driver is not connected. Call connect() before set_position_p_gain().")
        if value is None:
            if not self._default_p_gain:
                logger.warning("no captured Position_P_Gain defaults to restore; skipping")
                return
            if motors is None:
                self._bus.sync_write("Position_P_Gain", self._default_p_gain, normalize=False)
                self._p_gain = None
            else:
                # Restore ONLY the requested subset to their captured defaults.
                # Per-motor write (not sync_write): a partial Position_P_Gain dict
                # would need every motor backfilled (else real-hardware KeyError),
                # and backfilling would clobber the gains of motors we must leave
                # alone (e.g. legs while only the neck is restored).
                for motor in motors:
                    if motor in self._default_p_gain:
                        self._bus.write("Position_P_Gain", motor, self._default_p_gain[motor], normalize=False)
            return
        v = max(0, min(16383, int(value)))  # the servo's gain register is a 14-bit field
        if motors is None:
            self._bus.sync_write("Position_P_Gain", v, normalize=False)
            self._p_gain = v  # tracked value reflects the global gain only
        else:
            # Per-motor write so we touch only the subset; see the None branch above
            # for why a partial sync_write can't be used for a true subset.
            for motor in motors:
                self._bus.write("Position_P_Gain", motor, v, normalize=False)

    @property
    def position_p_gain(self) -> int | None:
        """Effective Position_P_Gain (the set value, else the captured default)."""
        if self._p_gain is not None:
            return self._p_gain
        if self._default_p_gain:
            return next(iter(self._default_p_gain.values()))
        return None

    def read_position_p_gain(self) -> dict[str, int]:
        """Read the live Position_P_Gain back from every servo (for verification)."""
        if self._bus is None:
            raise RuntimeError("Driver is not connected. Call connect() before read_position_p_gain().")
        raw = self._bus.sync_read("Position_P_Gain", normalize=False)
        return {n: int(v) for n, v in (raw or {}).items() if v is not None}

    @property
    def iir_enabled(self) -> bool:
        return self._iir_enabled

    @property
    def iir_alpha(self) -> float:
        return self._iir_alpha

    @staticmethod
    def _clamp_goal(name: str, tick: int) -> int:
        """Clamp a goal tick into the safe range, warning on each out-of-range write.

        The warning keeps the clamp from silently masking a caller bug; the engine
        stays in range, so normal use logs nothing.
        """
        clamped = max(SAFE_MIN_TICK, min(SAFE_MAX_TICK, tick))
        if clamped != tick:
            logger.warning(
                "goal %d for %r out of safe range [%d, %d]; clamped to %d",
                tick,
                name,
                SAFE_MIN_TICK,
                SAFE_MAX_TICK,
                clamped,
            )
        return clamped

    def read_positions(self) -> dict[str, int]:
        if self._bus is None:
            raise RuntimeError("Driver is not connected. Call connect() before read_positions().")
        # A failed read can return None for the whole batch or a single motor;
        # collapse both to a safe value (empty / NEUTRAL) so callers never hit
        # AttributeError / int(None), mirroring write_positions' None handling.
        # num_retry: this bus is flaky and a single read can drop; retrying keeps
        # callers like the timed return-to-neutral from falling back to a snap.
        raw = self._bus.sync_read("Present_Position", normalize=False, num_retry=3)
        if raw is None:
            return {}
        return {name: (NEUTRAL if tick is None else int(tick)) for name, tick in raw.items()}

    def read_telemetry(self, motors: Sequence[str] | None = None) -> ServoTelemetry:
        if self._bus is None:
            raise RuntimeError("Driver is not connected. Call connect() before read_telemetry().")
        # No num_retry: a retry re-reads every motor, and the caller polls often
        # enough that a dropped sweep costs less than the extra bus time would.
        sweep = self._bus.sync_read_span(TELEMETRY_REGISTERS, motors=motors)
        # A motor that did not answer is left out rather than filled in — unlike
        # read_positions, where a neutral stand-in is the safe guess. Inventing a
        # current or a voltage would be read as evidence that the motor is fine.
        return ServoTelemetry(
            current={n: v["Present_Current"] for n, v in sweep.values.items()},
            voltage={n: v["Present_Input_Voltage"] / _VOLTAGE_UNITS_PER_VOLT for n, v in sweep.values.items()},
            temperature={n: v["Present_Temperature"] for n, v in sweep.values.items()},
            silent=sweep.silent,
            unreached=sweep.unreached,
        )

    def set_profile_velocity_units(self, value: int, motors: list[str] | None = None) -> None:
        """Write Profile_Velocity in RAW register units (NOT ticks/s).

        On this hardware the profile is time-based, so the register value behaves
        like move-time (lower = faster; 0 = no profile). *motors* limits the write
        to a subset (e.g. the waving arm at 0 while the legs stay at 300); ``None``
        writes every motor. RAM, takes effect immediately.

        :meth:`set_profile_velocity` is the ABC's ticks/s contract; it converts
        through :meth:`_to_velocity_units` into these same raw register units,
        for callers that don't need this method's driver-specific escape hatch.
        """
        if self._bus is None:
            raise RuntimeError("Driver is not connected. Call connect() before set_profile_velocity_units().")
        v = max(0, int(value))
        if motors is None:
            self._bus.sync_write("Profile_Velocity", v, normalize=False)
        else:
            # Per-motor write so we touch only the subset (e.g. the waving arm at 0
            # while the legs stay at 300). A partial sync_write dict isn't usable
            # here — see set_position_p_gain for the real-hardware KeyError reason.
            for motor in motors:
                self._bus.write("Profile_Velocity", motor, v, normalize=False)

    def set_profile_velocity(self, ticks_per_second: int | dict[str, int]) -> None:
        if self._bus is None:
            raise RuntimeError("Driver is not connected. Call connect() before set_profile_velocity().")
        if isinstance(ticks_per_second, dict):
            # sync_write expects every registered motor; backfill un-named ones
            # with 0 (= default speed), the same defensive fill as write_positions.
            per_motor = {name: ticks_per_second.get(name, 0) for name in self._bus.motors}
            velocity: int | dict[str, int] = {n: self._to_velocity_units(t) for n, t in per_motor.items()}
            accel: int | dict[str, int] = {n: self._to_accel_units(t) for n, t in per_motor.items()}
        else:
            velocity = self._to_velocity_units(ticks_per_second)
            accel = self._to_accel_units(ticks_per_second)
        # Pair each velocity with an acceleration for a trapezoidal (eased) stop.
        # Accel first, so Profile_Velocity stays the last write observers see.
        self._bus.sync_write("Profile_Acceleration", accel, normalize=False)
        self._bus.sync_write("Profile_Velocity", velocity, normalize=False)

    def _to_velocity_units(self, ticks_per_second: int) -> int:
        """Convert ticks/s to Dynamixel Profile_Velocity register units.

        ``<= 0`` restores the driver's configured default speed (so callers can
        signal "back to normal" after a slow glide without knowing the value).
        """
        if ticks_per_second <= 0:
            return self._profile_velocity
        rev_per_min = (ticks_per_second / _TICKS_PER_REV) * 60.0
        return max(1, round(rev_per_min / _DXL_VELOCITY_UNIT_RPM))

    def _to_accel_units(self, ticks_per_second: int) -> int:
        """Profile_Acceleration register paired with a glide velocity.

        ``<= 0`` → 0 (rectangular profile), keeping gait and the post-glide restore
        snappy. Otherwise scale with velocity so the decel ramp is ~constant ~0.5s.
        """
        if ticks_per_second <= 0:
            return 0
        return max(1, round(self._to_velocity_units(ticks_per_second) / _ACCEL_RAMP_DIVISOR))
