# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Servo incident windows; see doc/explanation/servo-flight-recorder.md."""

import json
import logging
import os
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .hardware_error import describe_hardware_error
from .io.base import HardwareErrorRead


logger = logging.getLogger(__name__)


def _local_now() -> datetime:
    return datetime.now().astimezone()


def default_incident_dir() -> Path:
    """Resolve the incident directory from the current environment."""
    state = os.environ.get("XDG_STATE_HOME", "")
    root = Path(state) if state and Path(state).is_absolute() else Path.home() / ".local" / "state"
    return root / "palmimo" / "incidents"


@dataclass(frozen=True)
class AxisSample:
    """One axis's telemetry; None denotes a field not read in this sweep."""

    current: int | None
    position: int | None
    goal: int | None
    temperature: int | None
    voltage: float | None


@dataclass
class _Window:
    at: float
    wall: datetime
    motion: str
    deadline: float
    cap: float
    bits: dict[str, int] = field(default_factory=dict)
    clean: dict[str, float | None] = field(default_factory=dict)


@dataclass(frozen=True)
class _Sample:
    at: float
    tier: str
    motion: str
    axes: dict[str, AxisSample]
    hardware_errors: dict[str, int] = field(default_factory=dict)
    neck_at: float | None = None

    def row(self, trigger_at: float) -> dict[str, Any]:
        axes = {}
        for name, sample in self.axes.items():
            values = asdict(sample)
            values["gap"] = (
                sample.goal - sample.position if sample.goal is not None and sample.position is not None else None
            )
            axes[name] = values
        result: dict[str, Any] = {
            "kind": "sample",
            "tier": self.tier,
            "t": self.at - trigger_at,
            "motion": self.motion,
            "axes": axes,
            "hardware_errors": self.hardware_errors,
        }
        if self.tier == "context":
            result["t_neck"] = None if self.neck_at is None else self.neck_at - trigger_at
        return result


class ServoFlightRecorder:
    """Capture telemetry in memory and write an open incident only on flush()."""

    def __init__(
        self,
        incident_dir: Path,
        *,
        clock: Callable[[], float],
        wall_clock: Callable[[], datetime] = _local_now,
        fast_samples: int = 240,
        context_samples: int = 36,
        post_s: float = 2.0,
        post_max_s: float = 4.0,
        logger: logging.Logger = logger,
    ) -> None:
        self._incident_dir = incident_dir
        self._clock = clock
        self._wall_clock = wall_clock
        self._post_s = post_s
        self._post_max_s = post_max_s
        self._logger = logger
        self._lock = threading.Lock()
        self._fast: deque[_Sample] = deque(maxlen=fast_samples)
        self._context: deque[_Sample] = deque(maxlen=context_samples)
        self._baselined: set[str] = set()
        self._incident_servos: dict[str, int] = {}
        self._last_clean_at: dict[str, float] = {}
        self._window: _Window | None = None
        self._last_incident_path: Path | None = None

    @property
    def incident_dir(self) -> Path:
        return self._incident_dir

    @property
    def last_incident_path(self) -> Path | None:
        with self._lock:
            return self._last_incident_path

    @property
    def incident_servos(self) -> dict[str, int]:
        """Return a snapshot for context capture."""
        with self._lock:
            return dict(self._incident_servos)

    def prepare(self) -> None:
        """Inspect existing dumps and permissions without creating directories."""
        directory = self.incident_dir
        try:
            if not directory.exists():
                self._logger.info("Servo incidents will be written to %s", directory)
                return
            if not directory.is_dir() or not os.access(directory, os.W_OK):
                self._logger.warning("Incident directory is not writable; dumps will fail: %s", directory)
            dumps = list(directory.glob("incident-*.jsonl"))
            sizes = {path: path.stat().st_size for path in dumps}
            if len(dumps) > 100 or sum(sizes.values()) > 100_000_000:
                self._logger.warning(
                    "Incident directory holds %d dumps, %d bytes: %s", len(dumps), sum(sizes.values()), directory
                )
            leftovers = sorted(
                [path for path, size in sizes.items() if size == 0] + list(directory.glob("incident-*.jsonl.tmp"))
            )
            if leftovers:
                self._logger.warning("Found %d incomplete or unpublished incidents: %s", len(leftovers), leftovers[:5])
            self._logger.info("Servo incident directory: %s (%d dumps)", directory, len(dumps))
        except OSError as exc:
            self._logger.warning("Could not inspect incident directory %s: %s", directory, exc)

    def reset_for_connect(self) -> None:
        """Flush any open window and drop connection-local samples and baselines."""
        self.flush()
        with self._lock:
            self._fast.clear()
            self._context.clear()
            self._baselined.clear()
            self._last_clean_at.clear()
            self._window = None

    def capture_fast(self, now: float, motion: str, axes: Mapping[str, AxisSample]) -> None:
        with self._lock:
            self._fast.append(_Sample(now, "fast", motion, dict(axes)))

    def capture_context(
        self,
        now: float,
        motion: str,
        axes: Mapping[str, AxisSample],
        hardware_errors: Mapping[str, int],
        neck_at: float | None,
    ) -> None:
        with self._lock:
            self._context.append(_Sample(now, "context", motion, dict(axes), dict(hardware_errors), neck_at))

    def observe_hardware_errors(self, now: float, read: HardwareErrorRead, motion: str) -> None:
        with self._lock:
            previously_baselined = self._baselined.copy()
            for name, bits in read.errors.items():
                if name not in self._baselined:
                    self._baselined.add(name)
                    if bits:
                        known = self._incident_servos.get(name) == bits
                        self._incident_servos[name] = bits
                        if not known:
                            self._logger.error(
                                "%s already holds a latched hardware error (%s, 0x%02x) from before this connection; "
                                "no window recorded. Reboot or power-cycle the servo to clear it.",
                                name,
                                describe_hardware_error(bits),
                                bits,
                            )
            was_latched = bool(self._incident_servos)
            for name, bits in read.errors.items():
                if bits == 0:
                    self._incident_servos.pop(name, None)
                    self._last_clean_at[name] = now
            if was_latched and not self._incident_servos:
                self._logger.info("Servo flight recorder re-armed")
            for name, bits in read.errors.items():
                if name in self._incident_servos and bits != self._incident_servos[name]:
                    self._incident_servos[name] = bits
                    self._logger.error(
                        "Servo hardware error changed: %s %s (0x%02x)", name, describe_hardware_error(bits), bits
                    )
            new = {
                name: bits
                for name, bits in read.errors.items()
                if bits and name in previously_baselined and name not in self._incident_servos
            }
            if not new:
                return
            self._incident_servos.update(new)
            if self._window is None:
                self._window = _Window(now, self._wall_clock(), motion, now + self._post_s, now + self._post_max_s)
            else:
                self._window.deadline = min(now + self._post_s, self._window.cap)
            self._window.bits.update(new)
            self._window.clean.update({name: self._last_clean_at.get(name) for name in new})
            for name, bits in new.items():
                self._logger.error(
                    "Servo hardware error latched: %s %s (0x%02x) during %s; recording the incident window",
                    name,
                    describe_hardware_error(bits),
                    bits,
                    motion,
                )

    def window_due(self, now: float) -> bool:
        with self._lock:
            return self._window is not None and now >= self._window.deadline

    def flush(self) -> Path | None:
        """Write the open window outside the state lock; return only this dump's path."""
        with self._lock:
            window = self._window
            if window is None:
                return None
            self._window = None
            samples = [*self._fast, *self._context]
        started = self._clock()
        temporary: Path | None = None
        published: Path | None = None
        try:
            self.incident_dir.mkdir(parents=True, exist_ok=True)
            stem = f"incident-{window.wall:%Y%m%d-%H%M%S}"
            index = 0
            # Exclusive creation also reserves names against concurrent flushes.
            while True:
                suffix = f"-{index}" if index else ""
                published = self.incident_dir / f"{stem}{suffix}.jsonl"
                candidate = published.with_suffix(".jsonl.tmp")
                if published.exists():
                    index += 1
                    continue
                try:
                    stream = candidate.open("x", encoding="utf-8")
                except FileExistsError:
                    index += 1
                    continue
                temporary = candidate
                try:
                    collision = published.exists()
                except BaseException:
                    stream.close()
                    raise
                if collision:
                    stream.close()
                    temporary.unlink()
                    temporary = None
                    index += 1
                    continue
                break
            with stream:
                rows = [sample.row(window.at) for sample in sorted(samples, key=lambda sample: sample.at)]
                captured = max([0.0, *(row["t"] for row in rows)])
                clean = list(window.clean.values())
                last_clean = min(value for value in clean if value is not None) if clean and None not in clean else None
                header: dict[str, Any] = {
                    "kind": "header",
                    "schema": "palmimo-incident/1",
                    "ts": window.wall.isoformat(),
                    "monotonic_s": window.at,
                    "trigger": list(window.bits),
                    "trigger_bits": window.bits,
                    "motion": window.motion,
                    "units": {"current": "raw", "voltage": "V", "temperature": "C", "position": "tick"},
                    "last_clean_t": None if last_clean is None else last_clean - window.at,
                    "hardware_error_interval_s": 1.0,
                    "post_s": window.deadline - window.at,
                    "post_s_captured": captured,
                    "truncated": captured < window.deadline - window.at,
                }
                stream.write(json.dumps(header) + "\n")
                for row in rows:
                    stream.write(json.dumps(row) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.replace(temporary, published)
            except OSError as exc:
                self._logger.warning(
                    "Incident written but could not be published at %s; kept at %s: %s", published, temporary, exc
                )
                return self._remember(temporary)
            self._remember(published)
            try:
                self._sync_directory(self.incident_dir)
            except OSError as exc:
                self._logger.warning("Could not sync incident directory %s: %s", self.incident_dir, exc)
            self._logger.error(
                "Servo incident recorded: %s (%d samples; control loop stalled %.1f ms writing it)",
                published,
                len(samples),
                (self._clock() - started) * 1000,
            )
            return published
        except BaseException as exc:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    self._logger.warning("Could not remove incomplete incident %s: %s", temporary, cleanup_error)
            if not isinstance(exc, OSError):
                raise
            self._logger.warning(
                "Could not write servo incident dump to %s [%.1f ms]: %s",
                self.incident_dir,
                (self._clock() - started) * 1000,
                exc,
            )
            return None

    def _remember(self, path: Path) -> Path:
        with self._lock:
            self._last_incident_path = path
        return path

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        if os.name == "posix":
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
