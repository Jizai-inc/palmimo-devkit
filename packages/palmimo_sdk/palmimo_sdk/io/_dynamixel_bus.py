# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Direct Dynamixel serial bus over ``dynamixel_sdk``.

A thin, register-oriented wrapper around the Dynamixel Protocol 2.0 SDK, exposing
just the surface :class:`~palmimo_sdk.io.dynamixel.DynamixelDriver` needs
(``connect`` / ``sync_read`` / ``sync_write`` / ``write`` / torque control). It
gives the SDK core its own direct bus so it depends only on the lightweight
``dynamixel_sdk``.

Register names/addresses are the Dynamixel servo's control table (compatible
servo variants share these register addresses). Values are exchanged as raw
ticks; there is no normalization layer (the driver has always operated in raw ticks).
``import dynamixel_sdk`` is deferred to construction so ``import palmimo_sdk``
stays hardware-free.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 2.0
"""Dynamixel Protocol 2.0."""

_DEFAULT_TIMEOUT_MS = 1000
"""Packet timeout applied after connect."""

OPERATING_MODE_POSITION = 3
"""Operating_Mode register value for position control."""

_TORQUE_ENABLE = 1
_TORQUE_DISABLE = 0

# The servo's control table — {register: (address, size_bytes)}. Only the
# registers this codebase touches (SDK driver + maintainer reach-ins) are listed;
# an unlisted register raises KeyError loudly rather than misaddressing silently.
CONTROL_TABLE: dict[str, tuple[int, int]] = {
    "Return_Delay_Time": (9, 1),
    "Operating_Mode": (11, 1),
    "Torque_Enable": (64, 1),
    "Position_P_Gain": (84, 2),
    "Profile_Acceleration": (108, 4),
    "Profile_Velocity": (112, 4),
    "Goal_Position": (116, 4),
    "Present_Current": (126, 2),
    "Present_Position": (132, 4),
    "Present_Input_Voltage": (144, 2),
    "Present_Temperature": (146, 1),
}

# Registers whose value is two's-complement signed over its byte width (subset of
# the servo's encodings table for the registers we use).
SIGNED_REGISTERS = frozenset({"Goal_Position", "Present_Position", "Present_Current"})

# Model number reported by ping(), per supported model key.
MODEL_NUMBERS: dict[str, int] = {"xl330-m288": 1200, "xc330-m288": 1240}


def _encode_twos_complement(value: int, n_bytes: int) -> int:
    """Map a signed int to its unsigned two's-complement register value."""
    bit_width = n_bytes * 8
    if value >= 0:
        return value
    return (1 << bit_width) + value


def _decode_twos_complement(value: int, n_bytes: int) -> int:
    """Map an unsigned register value back to a signed int."""
    bits = n_bytes * 8
    sign_bit = 1 << (bits - 1)
    if value & sign_bit:
        value -= 1 << bits
    return value


def span_of(fields: Sequence[str]) -> tuple[int, int]:
    """Return the ``(start address, byte length)`` covering every register in *fields*.

    The span includes any registers that happen to sit between the requested
    ones — one request over a wider range costs less than several narrow ones.

    Raises:
        ValueError: *fields* is empty.
        KeyError: A name is not in :data:`CONTROL_TABLE`.
    """
    if not fields:
        raise ValueError("A span read needs at least one register name.")
    spans = [CONTROL_TABLE[name] for name in fields]
    start = min(addr for addr, _ in spans)
    end = max(addr + size for addr, size in spans)
    return start, end - start


def slice_field(buffer: Sequence[int], span_start: int, data_name: str) -> int:
    """Decode one register out of a *buffer* that was read starting at *span_start*."""
    addr, size = CONTROL_TABLE[data_name]
    offset = addr - span_start
    raw = int.from_bytes(bytes(buffer[offset : offset + size]), "little")
    if data_name in SIGNED_REGISTERS:
        return _decode_twos_complement(raw, size)
    return raw


@dataclass(frozen=True)
class SpanRead:
    """The outcome of one :meth:`DynamixelBus.sync_read_span` sweep.

    Motors answer one at a time in bus order and the SDK's reader stops at the
    first one that does not, so the motors after it were never asked. Keeping
    those two apart matters to any caller that counts failures per motor:
    *silent* is evidence about that motor, *unreached* is evidence about none.

    The mappings are typed read-only: a sweep is a record of what the bus said,
    and a caller that edits it is editing evidence.

    Attributes:
        values (Mapping[str, Mapping[str, int]]): Motor name -> register name -> value.
        silent (tuple[str, ...]): Motors that were asked and did not answer.
        unreached (tuple[str, ...]): Motors the sweep stopped short of.
    """

    values: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    silent: tuple[str, ...] = ()
    unreached: tuple[str, ...] = ()


def collect_span(
    names: Sequence[str],
    buffers: Mapping[str, Sequence[int]],
    fields: Sequence[str],
    span_start: int,
    span_length: int,
) -> SpanRead:
    """Split per-motor read buffers into answered / silent / unreached motors.

    Motors are judged in *names* order, which is the order they were asked in.
    A motor that answered holds a full-length buffer; the first one that does
    not is where the reader stopped, so everything after it was never asked.

    Args:
        names (Sequence[str]): Motors that were swept, in bus order.
        buffers (Mapping[str, Sequence[int]]): Motor name -> bytes read for it.
        fields (Sequence[str]): Registers to slice out of each buffer.
        span_start (int): Address the buffers start at.
        span_length (int): Byte length a complete answer has.
    """
    values: dict[str, dict[str, int]] = {}
    silent: list[str] = []
    unreached: list[str] = []
    for name in names:
        if silent:
            unreached.append(name)
            continue
        buffer = buffers.get(name) or ()
        if len(buffer) == span_length:
            values[name] = {f: slice_field(buffer, span_start, f) for f in fields}
        else:
            silent.append(name)
    return SpanRead(values, tuple(silent), tuple(unreached))


class DynamixelBus:
    """Minimal Dynamixel Protocol 2.0 bus over ``dynamixel_sdk``.

    Args:
        port (str): Serial port of the USB-Dynamixel bridge (e.g. ``/dev/ttyACM0``).
        motors (dict[str, int]): Motor name -> Dynamixel ID. Iteration order defines the read/write order.
        model (str): Model key (``"xc330-m288"`` / ``"xl330-m288"``); used for the connect
            handshake's model-number check.
        calibration (dict | None): Accepted for API parity and future use. Not applied: this
            bus exchanges raw ticks, so homing offsets are never used here.
    """

    def __init__(
        self,
        port: str,
        motors: dict[str, int],
        model: str,
        calibration: dict[str, Any] | None = None,
    ) -> None:
        import dynamixel_sdk as dxl

        self.port = port
        self.motors = motors
        self.model = model
        self.calibration = calibration
        self._dxl = dxl
        self._comm_success = dxl.COMM_SUCCESS
        self.port_handler = dxl.PortHandler(port)
        self.packet_handler = dxl.PacketHandler(PROTOCOL_VERSION)
        self.sync_reader = dxl.GroupSyncRead(self.port_handler, self.packet_handler, 0, 0)
        self.sync_writer = dxl.GroupSyncWrite(self.port_handler, self.packet_handler, 0, 0)

    @property
    def is_connected(self) -> bool:
        """bool: ``True`` if the serial port is open."""
        return self.port_handler.is_open

    def set_baudrate(self, baudrate: int) -> None:
        """Set the bus baud rate, verifying the change took effect.

        Raises:
            RuntimeError: The SDK failed to apply the requested baud rate.
        """
        if self.port_handler.getBaudRate() == baudrate:
            return
        self.port_handler.setBaudRate(baudrate)
        if self.port_handler.getBaudRate() != baudrate:
            raise RuntimeError(f"Failed to set bus baud rate to {baudrate}.")

    def connect(self) -> None:
        """Open the port, ping every expected motor, and set the packet timeout.

        Raises:
            ConnectionError: The port could not be opened.
            RuntimeError: A motor is missing or reports an unexpected model number.
        """
        try:
            if not self.port_handler.openPort():
                raise OSError(f"Failed to open port '{self.port}'.")
        except OSError as exc:
            raise ConnectionError(
                f"Could not connect on port '{self.port}'. Make sure it is the correct port."
            ) from exc
        self._handshake()
        self.port_handler.setPacketTimeoutMillis(_DEFAULT_TIMEOUT_MS)

    def disconnect(self, disable_torque: bool = True) -> None:
        """Close the port, optionally cutting torque on every motor first."""
        if disable_torque:
            self.port_handler.clearPort()
            self.port_handler.is_using = False
            self.disable_torque(num_retry=5)
        self.port_handler.closePort()

    def _handshake(self) -> None:
        """Ping every expected motor and verify presence and model number."""
        expected = MODEL_NUMBERS[self.model]
        missing: list[int] = []
        wrong: dict[int, int] = {}
        for motor_id in self.motors.values():
            found = self._ping(motor_id)
            if found is None:
                missing.append(motor_id)
            elif found != expected:
                wrong[motor_id] = found
        if missing or wrong:
            lines = [f"Motor check failed on port '{self.port}' (expected model {expected}):"]
            if missing:
                lines.append(f"  missing IDs: {missing}")
            if wrong:
                lines.append(f"  wrong model numbers (id: found): {wrong}")
            raise RuntimeError("\n".join(lines))

    def _ping(self, motor_id: int, num_retry: int = 0) -> int | None:
        """Ping a motor; return its model number, or ``None`` on failure."""
        for _ in range(1 + num_retry):
            model_number, comm, error = self.packet_handler.ping(self.port_handler, motor_id)
            if comm == self._comm_success:
                return model_number if error == 0 else None
        return None

    @contextmanager
    def torque_disabled(self) -> Iterator[None]:
        """Context manager: disable torque, run the body, re-enable on exit."""
        self.disable_torque()
        try:
            yield
        finally:
            self.enable_torque()

    def configure_motors(self) -> None:
        """Set every motor's Return_Delay_Time to 0 (minimum response delay)."""
        for motor in self.motors:
            self.write("Return_Delay_Time", motor, 0)

    def enable_torque(self, num_retry: int = 0) -> None:
        """Enable torque on every motor."""
        for motor in self.motors:
            self.write("Torque_Enable", motor, _TORQUE_ENABLE, num_retry=num_retry)

    def disable_torque(self, num_retry: int = 0) -> None:
        """Disable torque on every motor."""
        for motor in self.motors:
            self.write("Torque_Enable", motor, _TORQUE_DISABLE, num_retry=num_retry)

    def write(self, data_name: str, motor: str, value: int, *, normalize: bool = False, num_retry: int = 0) -> None:
        """Write one register on a single motor, waiting for a status packet.

        Slower but acknowledged; used for configuration and per-motor writes.

        Raises:
            NotImplementedError: *normalize* is True (this bus is raw-ticks only).
            ConnectionError / RuntimeError: The write failed after all retries.
        """
        _reject_normalize(normalize)
        addr, length = CONTROL_TABLE[data_name]
        motor_id = self.motors[motor]
        data = self._serialize(self._encode(data_name, int(value), length), length)
        comm = error = 0
        for _ in range(1 + num_retry):
            comm, error = self.packet_handler.writeTxRx(self.port_handler, motor_id, addr, length, data)
            if comm == self._comm_success:
                break
        self._check(comm, error, f"write '{data_name}' on id {motor_id}")

    def sync_write(
        self, data_name: str, values: int | dict[str, int], *, normalize: bool = False, num_retry: int = 0
    ) -> None:
        """Write one register across motors in a single broadcast packet.

        *values* is either a single value (applied to every motor) or a
        ``name -> value`` mapping. Faster than :meth:`write` but unacknowledged.

        Raises:
            NotImplementedError: *normalize* is True.
            ConnectionError: The packet failed to send after all retries.
        """
        _reject_normalize(normalize)
        addr, length = CONTROL_TABLE[data_name]
        ids_values = self._ids_values(values)
        self.sync_writer.clearParam()
        self.sync_writer.start_address = addr
        self.sync_writer.data_length = length
        for motor_id, value in ids_values.items():
            self.sync_writer.addParam(motor_id, self._serialize(self._encode(data_name, value, length), length))
        comm = 0
        for _ in range(1 + num_retry):
            comm = self.sync_writer.txPacket()
            if comm == self._comm_success:
                return
        raise ConnectionError(f"Failed to sync write '{data_name}' after {num_retry + 1} tries.")

    def sync_read(self, data_name: str, *, normalize: bool = False, num_retry: int = 0) -> dict[str, int | None] | None:
        """Read one register across all motors in a single request.

        Returns a ``name -> value`` mapping (a motor that did not respond maps to
        ``None``), or ``None`` if the whole batch failed after all retries — the
        driver treats both as a dropped read on this flaky bus.

        Raises:
            NotImplementedError: *normalize* is True.
        """
        _reject_normalize(normalize)
        addr, length = CONTROL_TABLE[data_name]
        self.sync_reader.clearParam()
        self.sync_reader.start_address = addr
        self.sync_reader.data_length = length
        for motor_id in self.motors.values():
            self.sync_reader.addParam(motor_id)
        comm = 0
        for _ in range(1 + num_retry):
            comm = self.sync_reader.txRxPacket()
            if comm == self._comm_success:
                break
        else:
            return None
        result: dict[str, int | None] = {}
        for name, motor_id in self.motors.items():
            if self.sync_reader.isAvailable(motor_id, addr, length):
                result[name] = self._decode(data_name, self.sync_reader.getData(motor_id, addr, length), length)
            else:
                result[name] = None
        return result

    def sync_read_span(
        self,
        fields: Sequence[str],
        *,
        motors: Sequence[str] | None = None,
        num_retry: int = 0,
    ) -> SpanRead:
        """Read several registers across motors in a single request.

        Reads the whole address range *fields* spans in one request and slices
        each register out per motor, so current, voltage and temperature cost
        one bus transaction rather than three — the difference between fitting
        in a control frame and not.

        Args:
            fields (Sequence[str]): Register names from :data:`CONTROL_TABLE`.
            motors (Sequence[str], optional): Motors to sweep; ``None`` sweeps every
                registered motor. The sweep always follows the bus's own motor
                order, whatever order *motors* is given in.
            num_retry (int): Extra attempts if the request fails to send. Only
                sending is retried; a motor that does not answer is reported
                rather than asked again, so one quiet servo cannot cost the
                sweep the answers it already has.

        Returns:
            SpanRead: The motors that answered, and how the rest failed to.

        Raises:
            ValueError: *fields* is empty.
            KeyError: An unknown register or motor name.
        """
        start, length = span_of(fields)
        names = self._sweep_names(motors)
        if not names:
            return SpanRead()
        self.sync_reader.clearParam()
        self.sync_reader.start_address = start
        self.sync_reader.data_length = length
        for name in names:
            self.sync_reader.addParam(self.motors[name])
        # Send and receive are driven separately so the two failures stay
        # distinguishable: if the request never goes out, no motor was asked,
        # and calling the first one in bus order silent would blacklist a
        # healthy servo every time the cable is loose.
        for _ in range(1 + num_retry):
            if self.sync_reader.txPacket() == self._comm_success:
                break
        else:
            return SpanRead(unreached=tuple(names))
        # Received once, not per retry: the reader overwrites its buffers in
        # place, so a second pass that fails early would drop answers the first
        # pass had already collected.
        self.sync_reader.rxPacket()
        # Read the reader's own buffers rather than isAvailable(): that helper
        # reports False for every motor as soon as any one of them fails, which
        # would throw away the answers collected before the failure.
        buffers = {name: self.sync_reader.data_dict.get(self.motors[name]) or () for name in names}
        return collect_span(names, buffers, fields, start, length)

    def _sweep_names(self, motors: Sequence[str] | None) -> list[str]:
        """Resolve *motors* to bus order, so a sweep and a re-check read alike."""
        if motors is None:
            return list(self.motors)
        wanted = set(motors)
        unknown = wanted - set(self.motors)
        if unknown:
            raise KeyError(f"Unknown motor(s) for a span read: {sorted(unknown)}")
        return [name for name in self.motors if name in wanted]

    def _ids_values(self, values: int | dict[str, int]) -> dict[int, int]:
        """Normalize *values* (scalar or name-dict) to an ``id -> int`` mapping."""
        if isinstance(values, dict):
            return {self.motors[name]: int(value) for name, value in values.items()}
        return {motor_id: int(values) for motor_id in self.motors.values()}

    @staticmethod
    def _encode(data_name: str, value: int, length: int) -> int:
        if data_name in SIGNED_REGISTERS:
            return _encode_twos_complement(value, length)
        return value

    @staticmethod
    def _decode(data_name: str, value: int, length: int) -> int:
        if data_name in SIGNED_REGISTERS:
            return _decode_twos_complement(value, length)
        return value

    def _serialize(self, value: int, length: int) -> list[int]:
        """Split an unsigned value into little-endian bytes for the SDK."""
        dxl = self._dxl
        if length == 1:
            return [value]
        if length == 2:
            return [dxl.DXL_LOBYTE(value), dxl.DXL_HIBYTE(value)]
        if length == 4:
            return [
                dxl.DXL_LOBYTE(dxl.DXL_LOWORD(value)),
                dxl.DXL_HIBYTE(dxl.DXL_LOWORD(value)),
                dxl.DXL_LOBYTE(dxl.DXL_HIWORD(value)),
                dxl.DXL_HIBYTE(dxl.DXL_HIWORD(value)),
            ]
        raise ValueError(f"Unsupported register byte size: {length}.")

    def _check(self, comm: int, error: int, what: str) -> None:
        """Raise if a single-motor comm result or packet error indicates failure."""
        if comm != self._comm_success:
            raise ConnectionError(f"Failed to {what}: {self.packet_handler.getTxRxResult(comm)}")
        if error != 0:
            raise RuntimeError(f"Failed to {what}: {self.packet_handler.getRxPacketError(error)}")


def _reject_normalize(normalize: bool) -> None:
    """This bus exchanges raw ticks; normalization is not supported."""
    if normalize:
        raise NotImplementedError("DynamixelBus operates in raw ticks; normalize=True is not supported.")
