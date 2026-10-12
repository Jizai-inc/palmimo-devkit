"""Shared servo doubles for flight-recorder lifecycle and sweep tests."""

from collections.abc import Callable, Sequence
from pathlib import Path

from palmimo_sdk import HardwareErrorRead, ServoDriver, ServoTelemetry
from palmimo_sdk.engine import MotionEngine
from palmimo_sdk.thermal import NECK_MOTORS


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FlightDriver(ServoDriver):
    def __init__(self, clock: Clock, directory: Path) -> None:
        self.clock = clock
        self.directory = directory
        self.connected = False
        self.bits = dict.fromkeys(MotionEngine().get_positions(), 0)
        self.events: list[tuple[str, float, bool]] = []
        self.writes: list[dict[str, int]] = []
        self.reads: list[tuple[float, tuple[str, ...]]] = []
        self.read_failure: Exception | None = None
        self.disconnect_failure = False
        self.neck_failure = False
        self.after_write: Callable[[], None] | None = None

    @property
    def is_connected(self) -> bool:
        return self.connected

    def connect(self) -> None:
        self.connected = True
        self.events.append(("connect", self.clock(), self.directory.exists()))

    def disconnect(self) -> None:
        self.events.append(("disconnect", self.clock(), self.directory.exists()))
        self.connected = False
        if self.disconnect_failure:
            raise OSError("Disconnected bus")

    def _write_positions(self, positions: dict[str, int]) -> None:
        self.writes.append(dict(positions))
        self.events.append(("write", self.clock(), self.directory.exists()))
        if self.after_write is not None:
            self.after_write()

    def read_positions(self) -> dict[str, int]:
        return dict.fromkeys(self.bits, 1900)

    def set_position_p_gain(self, value: int | None, motors: Sequence[str] | None = None) -> None:
        pass

    def read_telemetry(self, motors: Sequence[str] | None = None) -> ServoTelemetry:
        names = tuple(self.bits) if motors is None else tuple(motors)
        self.reads.append((self.clock(), names))
        if self.neck_failure and names == NECK_MOTORS:
            raise OSError("Unread neck")
        return ServoTelemetry(
            current=dict.fromkeys(names, 50),
            voltage=dict.fromkeys(names, 8.0),
            temperature=dict.fromkeys(names, 30),
            position=dict.fromkeys(names, 1900),
        )

    def read_hardware_errors(self, motors: Sequence[str] | None = None) -> HardwareErrorRead:
        self.events.append(("errors", self.clock(), self.directory.exists()))
        if self.read_failure is not None:
            raise self.read_failure
        names = tuple(self.bits) if motors is None else tuple(motors)
        return HardwareErrorRead({name: self.bits[name] for name in names})
