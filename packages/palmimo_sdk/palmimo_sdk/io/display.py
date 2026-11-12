# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Asset playback client for the face display protocol v2.

The wire contract is documented in ``doc/reference/display-protocol.md``.
The SDK owns playback and events; asset storage is managed by the platform.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import re
import threading
import time
import warnings
import zlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn

from ..reservation import Reservation, acquire_resource
from ._timeout import ProbeTimeoutError, run_with_timeout


logger = logging.getLogger(__name__)


# USB vendor id the face-display MCU (pico-sdk stdio_usb CDC) enumerates as.
# Used to pick the face out of the ACM ports — the servo bus is a *different*
# vendor, so matching this avoids commanding the wrong device.
DISPLAY_USB_VID = 0x2E8A  # Raspberry Pi

# Firmware serial-protocol settings: 115200 8N1, \n.
BAUDRATE = 115200

# Deadline for the whole connect sequence (port detection + serial open). A real
# face display's USB-CDC port opens near-instantly; 5s is wide margin while still
# failing fast when no face is attached or the matched port is some other device
# (see _timeout.py).
_CONNECT_TIMEOUT_S = 5.0

STANDARD_CATALOG_PATH = Path("/usr/local/share/palmimo/display-assets/catalog.json")
APP_ASSETS_ROOT = Path("/run/palmimo/apps")
_ASSET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[a-z0-9][a-z0-9._-]{0,39}")
_APP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
_SLOT_ID = re.compile(r"[a-z0-9_]{1,32}")
_CRC = re.compile(r"[0-9a-f]{8}")
_KINDS = {"vector", "image", "gif"}


class FaceDisplayError(RuntimeError):
    """Raised on a serial/protocol failure talking to the face display."""


class FaceDisplayConnectTimeoutError(FaceDisplayError):
    """Raised when connecting to the face display exceeds :data:`_CONNECT_TIMEOUT_S`.

    Distinct from the plain :class:`FaceDisplayError` :meth:`FaceDisplay.connect`
    raises when no device is found or the open call itself fails immediately:
    this fires when a port WAS resolved but opening it never completed -- e.g.
    no face attached, or the matched port belongs to a different device.
    """


class _FirmwareReplyError(FaceDisplayError):
    """A consumed firmware error leaves the line protocol synchronized."""


class FaceDisplayFirmwareError(FaceDisplayError):
    """Firmware is incompatible; the platform must switch to protocol 2."""


def find_face_port() -> str | None:
    """Return the serial device path of the attached face display, or ``None``.

    Scans the system's serial ports for the face-display USB vendor id
    (:data:`DISPLAY_USB_VID`). On a Pi the face and the servo bus both
    appear as ``/dev/ttyACM*``; matching the vendor id picks the face, not the
    bus. If several face displays are present the first is returned (log a warning).
    """
    from serial.tools import list_ports

    matches = [p for p in list_ports.comports() if getattr(p, "vid", None) == DISPLAY_USB_VID]
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "multiple face-display USB devices found (%s); using the first",
            ", ".join(p.device for p in matches),
        )
    return matches[0].device


class FaceDisplay:
    """Serial client for protocol 2 asset playback.

    Args:
        port: Serial device, or None for USB vendor auto-detection.
        timeout: Command deadline in seconds, including transfer progress.
        on_power_event: Optional power callback, called on the reader thread.
            Keep callbacks short and thread-safe; enqueue work on your event loop.
        serial_factory: Injectable serial transport factory for hardware-free use.
        connect_timeout: Deadline for port detection and opening, in seconds.
            HELLO additionally uses the command timeout.

    Synchronous commands from reader callbacks raise FaceDisplayError. The client
    closes on I/O failure, timeout, or malformed replies; reconnect before reuse.
    """

    def __init__(
        self,
        port: str | None = None,
        *,
        timeout: float = 1.0,
        on_power_event: Callable[[bool], None] | None = None,
        serial_factory: Callable[..., Any] | None = None,
        connect_timeout: float = _CONNECT_TIMEOUT_S,
    ) -> None:
        if timeout <= 0 or not math.isfinite(timeout):
            raise ValueError("timeout must be finite and positive")
        self._port = port
        self._timeout = timeout
        self._on_power_event = on_power_event
        self._serial_factory = serial_factory
        self._connect_timeout = connect_timeout
        self._ser: Any = None
        self._reservation: Reservation | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._io_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._reply_queue: queue.Queue[str | FaceDisplayError] = queue.Queue()
        self._on_tap: Callable[[int, int], None] | None = None
        self._catalog: list[dict[str, Any]] | None = None
        self._app_id: str | None = None

    # ---- lifecycle ----------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._ser is not None

    def connect(self) -> None:
        """Open, reserve, and check HELLO; reject firmware other than protocol 2."""
        if threading.current_thread() is self._reader:
            raise FaceDisplayError("Cannot connect from a display callback")
        with self._io_lock:
            self._connect()

    def _connect(self) -> None:
        if self.is_connected:
            return

        if self._reader is not None and self._reader.is_alive():
            if self._reader is threading.current_thread():
                raise FaceDisplayError("Cannot reconnect from a display callback")
            self._reader.join(timeout=self._timeout + 0.5)
            if self._reader.is_alive():
                raise FaceDisplayError("Previous display reader has not stopped")
        self._reply_queue = queue.Queue()
        self._stop = threading.Event()
        self._catalog = None
        app_id = os.environ.get("PALMIMO_APP_ID")
        self._app_id = app_id if app_id and "." in app_id and _APP_ID.fullmatch(app_id) else None
        factory = self._serial_factory
        if factory is None:
            import serial  # lazy: keeps `import palmimo_sdk` dependency-free

            factory = serial.Serial

        # Recorded the instant port resolution succeeds, so the timeout error
        # can name the real port even when the hang happened later.
        resolved_port: list[str] = []

        def _open() -> tuple[str, Any, Reservation]:
            port = self._port or find_face_port()
            if port is None:
                raise FaceDisplayError(
                    "No face display found (no USB device with vendor id "
                    f"{DISPLAY_USB_VID:#06x}). Pass port=... explicitly, or check the cable."
                )
            resolved_port.append(port)
            reservation = acquire_resource("display")
            try:
                ser = factory(port, BAUDRATE, timeout=min(self._timeout, 0.1))
            except BaseException as exc:
                reservation.release()
                if not isinstance(exc, Exception):
                    raise
                raise FaceDisplayError(f"Could not open face display on {port!r}: {exc}") from exc
            return port, ser, reservation

        def _on_late_open(opened: tuple[str, Any, Reservation]) -> None:
            # Connect finished after the timeout already raised: nobody owns
            # this serial object, so close it instead of leaking an open port.
            late_port, ser, reservation = opened
            logger.warning(
                "Face display connect on %r finished after its %.1fs timeout had already fired; "
                "closing the orphaned, late-arriving serial port.",
                late_port,
                self._connect_timeout,
            )
            try:
                ser.close()
            except Exception:
                logger.exception("Late display close failed; retaining display reservation")
            else:
                reservation.release()

        previous_reservation = self._reservation
        try:
            port, self._ser, self._reservation = run_with_timeout(
                _open, timeout=self._connect_timeout, on_late_result=_on_late_open
            )
        except ProbeTimeoutError as exc:
            port_desc = resolved_port[0] if resolved_port else (self._port or "auto-detected face-display port")
            raise FaceDisplayConnectTimeoutError(
                f"Timed out connecting to the face display on {port_desc!r} after "
                f"{self._connect_timeout:.1f}s (no response opening the serial port). Check that "
                "the face display is powered and this is the correct port."
            ) from exc
        if previous_reservation is not None:
            previous_reservation.release()
        self._port = port

        self._reader = threading.Thread(
            target=self._read_loop,
            args=(self._ser, self._stop, self._reply_queue),
            name="face-evt",
            daemon=True,
        )
        try:
            try:
                self._ser.write_timeout = self._timeout
            except Exception as exc:
                raise FaceDisplayError(f"Could not configure face display: {exc}") from exc
            self._reader.start()
            reply = self._command("HELLO", "OK HELLO ", allow_error=True)
            if reply == "ERR HELLO (try LIST)" or (
                reply.startswith("OK HELLO ") and self._fields(reply[9:]).get("proto") != "2"
            ):
                raise FaceDisplayFirmwareError(
                    "Face display requires protocol 2. The platform must switch the display firmware "
                    "before starting this app (palmimo-display-firmware desire 2 --by app-start, then ensure)."
                )
            if reply.startswith("ERR "):
                raise FaceDisplayError(reply)
            if set(self._fields(reply[9:])) != {"proto", "fw", "caps"}:
                self._protocol_error("Invalid HELLO fields")
        except BaseException:
            self.disconnect()
            raise

    def _drop(self, ser: Any) -> None:
        with self._state_lock:
            if self._ser is not ser:
                return
            self._stop.set()
            self._ser = None
            self._catalog = None
            ser.close()
            if self._reservation is not None:
                self._reservation.release()
                self._reservation = None

    def disconnect(self) -> None:
        """Close the port, stop events, and discard the connection's catalog.

        A failed close retains the reservation until a successful reconnect.
        Calling from an event callback is supported.
        """
        with self._state_lock:
            reader, stop = self._reader, self._stop
            try:
                if self._ser is not None:
                    self._drop(self._ser)
            finally:
                stop.set()
        if reader is not None and reader.ident is not None and reader is not threading.current_thread():
            reader.join(timeout=self._timeout + 0.5)

    def __enter__(self) -> FaceDisplay:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()

    def play(self, id: str, hold_ms: int | None = None) -> str:
        """Play a standard asset or a present, described asset belonging to this app.

        Standard names are always permitted. A positive hold_ms returns to idle
        afterwards; None holds until the next command.
        """
        self._validate_id(id)
        owner = id.split("/", 1)[0]
        if owner != "palmimo" and (owner != self._app_id or not any(asset["id"] == id for asset in self.catalog())):
            raise ValueError("Asset is outside the standard and current app catalog")
        if hold_ms is not None and (type(hold_ms) is not int or hold_ms <= 0):
            raise ValueError("hold_ms must be a positive integer or None")
        command = f"PLAY {id}" + (f" {hold_ms}" if hold_ms is not None else "")
        return self._command(command, f"OK PLAY {id}")

    def set_expression(self, name: str, hold_ms: int = 0) -> str:
        """Deprecated alias for play('palmimo/' + name.lower())."""
        warnings.warn("set_expression() is deprecated; use play()", DeprecationWarning, stacklevel=2)
        return self.play("palmimo/" + name.lower(), hold_ms=None if hold_ms == 0 else hold_ms)

    def stop(self) -> str:
        """Return to idle and reset all slot overrides."""
        return self._command("STOP", "OK STOP")

    def idle(self) -> str:
        """Compatibility alias for stop()."""
        return self.stop()

    def wake(self) -> str:
        """Play the wake screen, followed by idle."""
        return self._command("WAKE", "OK WAKE")

    def power(self, on: bool) -> str:
        """Set the display power state."""
        state = "ON" if on else "OFF"
        return self._command(f"POWER {state}", f"OK POWER {state}")

    def set_brightness(self, percent: int) -> str:
        """Set backlight brightness from 0 to 100."""
        if type(percent) is not int or not 0 <= percent <= 100:
            raise ValueError("Brightness must be an integer from 0 to 100")
        return self._command(f"BL {percent}", f"OK BL {percent}")

    def param(self, sid: str, *values: float) -> str:
        """Override a playing vector slot; NOSLOT logs a warning and returns its reply."""
        if not _SLOT_ID.fullmatch(sid) or not values or any(not math.isfinite(v) for v in values):
            raise ValueError("PARAM requires a slot id and finite numeric values")
        numbers = " ".join(format(Decimal(str(v)), "f") for v in values)
        reply = self._command(f"PARAM {sid} {numbers}", f"OK PARAM {sid}", allow_error=True)
        if reply == "ERR PARAM NOSLOT":
            logger.warning("Display slot %s is absent from the playing asset", sid)
        elif reply.startswith("ERR "):
            raise FaceDisplayError(reply)
        return reply

    def on_tap(self, callback: Callable[[int, int], None] | None) -> None:
        """Set the tap callback (panel x, y, 0–479), or None to unsubscribe.

        Called on the reader thread. Do not perform heavy work or synchronous
        display commands here; dispatch that work to your application event loop.
        """
        self._on_tap = callback

    def ping(self) -> bool:
        """Return True after PONG; failures raise FaceDisplayError."""
        return self._command("PING", "PONG") == "PONG"

    def version(self) -> str:
        """Return the firmware's version string."""
        return self._command("VERSION", "VERSION ")[8:]

    def stat(self) -> dict[str, str]:
        """Return the firmware status key/value fields."""
        reply = self._command("STAT", "STAT ")
        fields = self._fields(reply[5:])
        if fields.get("power") not in ("ON", "OFF") or fields.get("proto") != "2":
            self._protocol_error("Invalid STAT fields")
        return fields

    def list_expressions(self) -> str:
        """Deprecated: return space-separated standard names from the live catalog."""
        warnings.warn("list_expressions() is deprecated; use catalog()", DeprecationWarning, stacklevel=2)
        return " ".join(a["id"].split("/", 1)[1] for a in self.catalog() if a["id"].startswith("palmimo/"))

    @staticmethod
    def _validate_id(id: str) -> None:
        if not _ASSET_ID.fullmatch(id) or len(id) > 127:
            raise ValueError("Invalid display asset id")

    def _fields(self, text: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for token in text.split(" "):
            key, sep, value = token.partition("=")
            if not sep or not key or not value or key in result:
                self._protocol_error("Malformed display fields")
            result[key] = value
        return result

    def _cursor(self, value: str, current: int) -> int | None:
        if value == "-":
            return None
        if not value.isascii() or not value.isdecimal() or int(value) <= current:
            self._protocol_error("Invalid asset pagination cursor")
        return int(value)

    def _asset(self, id: str, kind: str, size: str, crc: str) -> dict[str, Any]:
        try:
            self._validate_id(id)
        except ValueError:
            self._protocol_error("Invalid asset id in reply")
        if kind not in _KINDS or not size.isascii() or not size.isdecimal() or not _CRC.fullmatch(crc):
            self._protocol_error("Invalid asset metadata")
        return {"id": id, "kind": kind, "bytes": int(size), "crc32": crc}

    def assets(self) -> list[dict[str, Any]]:
        """Return all ASSET LIST pages as id/kind/bytes/crc32 dictionaries."""
        self._check_caller()
        with self._io_lock:
            return self._assets()

    def _assets(self) -> list[dict[str, Any]]:
        """Return all ASSET LIST pages as id/kind/bytes/crc32 dictionaries."""
        result: dict[str, dict[str, Any]] = {}
        index = 0
        while True:
            reply = self._command("ASSET LIST" + (f" {index}" if index else ""), "OK ASSET LIST ")
            tokens = reply[14:].split(" ")
            if not tokens[0].startswith("next="):
                self._protocol_error("Missing ASSET LIST cursor")
            for token in tokens[1:]:
                parts = token.split(":")
                if len(parts) != 4:
                    self._protocol_error("Malformed ASSET LIST entry")
                asset = self._asset(*parts)
                if asset["id"] in result:
                    self._protocol_error("Duplicate ASSET LIST id")
                result[asset["id"]] = asset
            next_index = self._cursor(tokens[0][5:], index)
            if next_index is None:
                return list(result.values())
            index = next_index

    def asset_info(self, id: str) -> dict[str, Any]:
        """Return metadata and every slot page; params maps slot ids to dimensions."""
        self._check_caller()
        with self._io_lock:
            return self._asset_info(id)

    def _asset_info(self, id: str) -> dict[str, Any]:
        """Return metadata and every slot page; params maps slot ids to dimensions."""
        self._validate_id(id)
        result: dict[str, Any] | None = None
        params: dict[str, int] = {}
        index = 0
        while True:
            prefix = f"OK ASSET INFO {id} "
            reply = self._command(f"ASSET INFO {id}" + (f" {index}" if index else ""), prefix)
            fields = self._fields(reply[len(prefix) :])
            if set(fields) != {"kind", "bytes", "crc32", "next", "params"}:
                self._protocol_error("Invalid ASSET INFO fields")
            asset = self._asset(id, fields["kind"], fields["bytes"], fields["crc32"])
            if result is not None and result != asset:
                self._protocol_error("Asset metadata changed across pages")
            result = asset
            if fields["params"] != "-":
                for slot in fields["params"].split(","):
                    sid, sep, dim = slot.partition("/")
                    if (
                        not sep
                        or not _SLOT_ID.fullmatch(sid)
                        or not dim.isascii()
                        or not dim.isdecimal()
                        or not 1 <= int(dim) <= 144
                        or sid in params
                    ):
                        self._protocol_error("Invalid asset slot metadata")
                    params[sid] = int(dim)
            next_index = self._cursor(fields["next"], index)
            if next_index is None:
                return {**result, "params": params}
            index = next_index

    def stream_frame(self, w: int, h: int, rgb565_bytes: bytes) -> str:
        """Show one RGB565 little-endian frame, with acknowledged 16 KiB flow control."""
        if type(w) is not int or type(h) is not int or not (1 <= w <= 169 and 1 <= h <= 169):
            raise ValueError("STREAM dimensions must be integers from 1 to 169")
        if len(rgb565_bytes) != w * h * 2:
            raise ValueError("STREAM requires exactly w * h * 2 bytes")
        with self._transaction() as deadline:
            total = len(rgb565_bytes)
            self._write(f"STREAM {w} {h} RGB565 {zlib.crc32(rgb565_bytes):08x}\n".encode(), deadline)
            begin = self._reply(deadline)
            self._check_error(begin)
            if begin != f"OK XFER BEGIN {total} ACK=4096":
                self._protocol_error("Invalid STREAM transfer begin")
            sent = acknowledged = 0
            while acknowledged < total:
                limit = min(total, acknowledged + 16384)
                if sent < limit:
                    self._write(rgb565_bytes[sent:limit], deadline)
                    sent = limit
                line = self._reply(deadline)
                self._check_error(line)
                expected = min(total, acknowledged + 4096)
                if line != f"ACK {expected}" or expected > sent:
                    self._protocol_error("Invalid STREAM acknowledgement")
                acknowledged = expected
            reply = self._reply(deadline)
            self._check_error(reply)
            if reply != "OK STREAM":
                self._protocol_error("Invalid STREAM final reply")
            return reply

    def catalog(self, *, tags: Iterable[str] = (), locale: str | None = None) -> list[dict[str, Any]]:
        """Return present, described standard/current-app assets matching every tag.

        Entries include id, kind, description, tags, params (description/dim), and
        translations. Descriptions select exact locale, base language, then source.
        Declarations and live metadata are cached until disconnect/reconnect.
        """
        self._check_caller()
        with self._io_lock:
            ser = self._ser
            if self._catalog is None:
                entries = self._load_catalog()
                with self._state_lock:
                    if self._ser is not ser:
                        raise FaceDisplayError("Display disconnected while loading catalog")
                    self._catalog = entries
            sources = self._catalog
        requested = set(tags)
        result = []
        for source in sources:
            if not requested.issubset(source["tags"]):
                continue
            entry = json.loads(json.dumps(source))
            translations = entry["translations"]

            def localized(key: str, original: str, translations: dict[str, Any] = translations) -> str:
                if locale:
                    for candidate in (locale.lower(), locale.lower().split("-", 1)[0]):
                        value = translations.get(candidate, {}).get(key)
                        if isinstance(value, str) and value.strip():
                            return value
                return original

            entry["description"] = localized("description", entry["description"])
            for sid, param in entry["params"].items():
                param["description"] = localized(f"params.{sid}", param["description"])
            result.append(entry)
        return result

    def _declarations(self, path: Path, owner: str) -> list[dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("assets"), list):
                raise ValueError("Expected catalog schema 1 with an assets array")
        except (OSError, ValueError) as exc:
            logger.warning("Display catalog unavailable at %s: %s", path, exc)
            return []
        result = []
        for entry in data["assets"]:
            if not isinstance(entry, dict):
                logger.warning("Ignoring malformed display catalog entry at %s", path)
                continue
            id = entry.get("id")
            description = entry.get("description")
            tags = entry.get("tags", [])
            params = entry.get("params", {})
            translations = entry.get("translations", {})
            if (
                not isinstance(id, str)
                or not _ASSET_ID.fullmatch(id)
                or len(id) > 127
                or id.split("/", 1)[0] != owner
                or not isinstance(description, str)
                or not description.strip()
                or not isinstance(tags, list)
                or any(not isinstance(tag, str) for tag in tags)
                or not isinstance(params, dict)
                or any(
                    not isinstance(sid, str)
                    or not _SLOT_ID.fullmatch(sid)
                    or not isinstance(param, dict)
                    or not isinstance(param.get("description"), str)
                    or not param["description"].strip()
                    for sid, param in params.items()
                )
                or not isinstance(translations, dict)
            ):
                logger.warning("Ignoring malformed or unowned display catalog entry at %s", path)
                continue
            translations = {loc: fields for loc, fields in translations.items() if isinstance(fields, dict)}
            result.append(
                {"id": id, "description": description, "tags": tags, "params": params, "translations": translations}
            )
        return result

    def _load_catalog(self) -> list[dict[str, Any]]:
        declarations = self._declarations(STANDARD_CATALOG_PATH, "palmimo")
        if self._app_id:
            declarations += self._declarations(APP_ASSETS_ROOT / self._app_id / "assets.json", self._app_id)
        present = {asset["id"] for asset in self.assets()}
        missing = sum(entry["id"] not in present for entry in declarations)
        if missing:
            logger.warning(
                "Omitting %d display catalog assets: absent from firmware (not synchronized or sync failed)", missing
            )
        result: dict[str, dict[str, Any]] = {}
        for entry in declarations:
            if entry["id"] not in present:
                continue
            info = self.asset_info(entry["id"])
            params = {
                sid: {"description": param["description"], "dim": info["params"][sid]}
                for sid, param in entry["params"].items()
                if sid in info["params"]
            }
            result[entry["id"]] = {**entry, "kind": info["kind"], "params": params}
        return list(result.values())

    def _check_caller(self) -> None:
        if threading.current_thread() is self._reader:
            raise FaceDisplayError("Synchronous display commands cannot run on the reader callback thread")
        if self._ser is None:
            raise FaceDisplayError("Not connected. Call connect() before sending commands.")

    def _protocol_error(self, message: str) -> NoReturn:
        self.disconnect()
        raise FaceDisplayError(message)

    def _write(self, payload: bytes, deadline: float) -> None:
        self._check_caller()
        ser = self._ser
        offset = 0
        try:
            while offset < len(payload):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Display write deadline exceeded")
                ser.write_timeout = remaining
                count = ser.write(payload[offset:])
                if not isinstance(count, int) or not 0 < count <= len(payload) - offset:
                    raise OSError("Invalid serial write count")
                offset += count
        except Exception as exc:
            self.disconnect()
            raise FaceDisplayError(f"Display write failed: {exc}") from exc

    def _reply(self, deadline: float) -> str:
        try:
            reply = self._reply_queue.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            self._protocol_error("Display reply timed out; reconnect before sending another command")
        if isinstance(reply, FaceDisplayError):
            self._protocol_error(str(reply))
        return reply

    def _check_error(self, reply: str, *, allow_error: bool = False) -> None:
        if reply == "ERR XFER TIMEOUT":
            self._protocol_error(reply)
        if reply.startswith("ERR ") and not allow_error:
            raise _FirmwareReplyError(reply)

    @contextmanager
    def _transaction(self) -> Iterator[float]:
        self._check_caller()
        with self._io_lock:
            self._check_caller()
            try:
                yield time.monotonic() + self._timeout
            except _FirmwareReplyError:
                raise
            except BaseException:
                self.disconnect()
                raise

    def _command(self, line: str, expected: str, *, allow_error: bool = False) -> str:
        with self._transaction() as deadline:
            self._write((line + "\n").encode("ascii"), deadline)
            reply = self._reply(deadline)
            self._check_error(reply, allow_error=allow_error)
            if reply.startswith("ERR "):
                return reply
            if not (reply.startswith(expected) if expected.endswith(" ") else reply == expected):
                self._protocol_error(f"Unexpected display reply to {line!r}: {reply!r}")
            return reply

    def _read_loop(self, ser: Any, stop: threading.Event, replies: queue.Queue[str | FaceDisplayError]) -> None:
        pending = bytearray()
        try:
            while not stop.is_set():
                raw = ser.readline()
                if not raw:
                    continue
                pending.extend(raw)
                while b"\n" in pending:
                    raw_line, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    if len(raw_line) + 1 > 1024 or b"\r" in raw_line:
                        raise ValueError("Invalid display line framing")
                    line = raw_line.decode("ascii")
                    if not line or any(ord(c) < 32 or ord(c) > 126 for c in line):
                        raise ValueError("Invalid display reply characters")
                    if line.startswith("EVT "):
                        self._event(line)
                    else:
                        replies.put(line)
                if len(pending) >= 1024:
                    raise ValueError("Display line exceeds 1024 bytes")
        except Exception as exc:
            replies.put(FaceDisplayError(f"Display read failed: {exc}"))
            try:
                self._drop(ser)
            except Exception:
                logger.exception("Display reader cleanup failed; retaining display reservation")

    def _event(self, line: str) -> None:
        parts = line.split(" ")
        try:
            if len(parts) == 4 and parts[:2] == ["EVT", "TAP"] and self._on_tap:
                x, y = int(parts[2]), int(parts[3])
                if 0 <= x <= 479 and 0 <= y <= 479:
                    self._on_tap(x, y)
            elif line in ("EVT POWER ON", "EVT POWER OFF") and self._on_power_event:
                self._on_power_event(line == "EVT POWER ON")
        except Exception:
            logger.exception("Display event callback failed")
