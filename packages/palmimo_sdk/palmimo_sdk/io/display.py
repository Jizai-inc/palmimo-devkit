# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""FaceDisplay — the thin host-side client for the face display.

The face is its own microcontroller (the face-display MCU) running the
face-display firmware and exposing a 1-line serial contract over USB-CDC. Emotion
*decisions* live on the host (Pi/PC); this class just speaks that contract — one
method per firmware command (``set_expression`` → ``EXPR``, ``wake`` → ``WAKE``,
…). The display draws; we tell it what.

    from palmimo_sdk import FaceDisplay

    with FaceDisplay() as face:        # auto-detects the face-display USB-CDC port
        face.wake()                    # boot animation → yellow IDLE
        face.set_expression("happy", hold_ms=3000)   # 3s smile, then auto-IDLE

The display also *emits* one event: a screen double-tap → power-toggle confirm
dialog sends ``EVT POWER ON|OFF``. Pass ``on_power_event=`` to react to it (the
read thread invokes the callback off the main thread).

``pyserial`` is a base dependency (the ``palmimo-sdk[face]`` extra names it so a
project driving the face declares what it uses). It is imported lazily in
:meth:`connect`, so compute-only use never opens the serial stack — mirroring
how :class:`DynamixelDriver` defers ``dynamixel_sdk``.

Protocol contract: the face-display firmware's README, published with the
firmware release.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

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

# The 12 expressions baked into the firmware. Aliases the
# firmware also accepts (GRIN, JOY, …) are resolved on-device, so they work too;
# this set is just what `LIST` would return and what we validate against.
EXPRESSIONS = (
    "HAPPY",
    "SMILE",
    "LAUGH",
    "LOVE",
    "SAD",
    "ANGRY",
    "SURPRISED",
    "THINKING",
    "SLEEPY",
    "SLEEP",
    "IDEA",
    "HEART",
)


class FaceDisplayError(RuntimeError):
    """Raised on a serial/protocol failure talking to the face display."""


class FaceDisplayConnectTimeoutError(FaceDisplayError):
    """Raised when connecting to the face display exceeds :data:`_CONNECT_TIMEOUT_S`.

    Distinct from the plain :class:`FaceDisplayError` :meth:`FaceDisplay.connect`
    raises when no device is found or the open call itself fails immediately:
    this fires when a port WAS resolved but opening it never completed -- e.g.
    no face attached, or the matched port belongs to a different device.
    """


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
    """Host-side serial client for the face display.

    Args:
        port (str | None): Serial device of the face (e.g. ``/dev/ttyACM0`` / ``COM3``). When
            ``None`` (default) the port is auto-detected by USB vendor id at
            :meth:`connect` time (see :func:`find_face_port`).
        timeout (float): Read timeout in seconds for command replies / event polling.
        on_power_event (callable | None): Called as ``on_power_event(power_on: bool)`` when the display sends
            ``EVT POWER ON|OFF`` (screen double-tap → confirm dialog YES). When set,
            a daemon reader thread starts on :meth:`connect`; the callback runs on
            that thread, so keep it short / thread-safe.
        serial_factory (callable): Seam for tests — builds the underlying serial object. Defaults to
            ``serial.Serial``; a fake implementing ``write``/``readline``/``close``
            can be injected without hardware.
        connect_timeout (float): Seconds before :meth:`connect` gives up with
            :class:`FaceDisplayConnectTimeoutError` instead of hanging (default
            :data:`_CONNECT_TIMEOUT_S`; contract details in :mod:`palmimo_sdk.io._timeout`).
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
        self._port = port
        self._timeout = timeout
        self._on_power_event = on_power_event
        self._serial_factory = serial_factory
        self._connect_timeout = connect_timeout
        self._ser: Any = None
        # The reader thread (when on_power_event is set), a lock that serializes
        # command writes + their reply wait, and the queue the reader hands
        # command replies back on (reads happen only on the reader thread).
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._io_lock = threading.Lock()
        self._reply_queue: queue.Queue[str] = queue.Queue()

    # ---- lifecycle ----------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._ser is not None

    def connect(self) -> None:
        """Open the serial port (auto-detecting it when *port* was ``None``).

        Starts the ``EVT POWER`` reader thread when an ``on_power_event``
        callback was given. Raises :class:`FaceDisplayError` if no face can be
        found or the port cannot be opened, and
        :class:`FaceDisplayConnectTimeoutError` (a subclass of
        :class:`FaceDisplayError`) if port auto-detection + the serial open
        together exceed :attr:`_connect_timeout` seconds — see
        :mod:`palmimo_sdk.io._timeout`.
        """
        if self.is_connected:
            return

        factory = self._serial_factory
        if factory is None:
            import serial  # lazy: keeps `import palmimo_sdk` dependency-free

            factory = serial.Serial

        # Recorded the instant port resolution succeeds, so the timeout error
        # can name the real port even when the hang happened later.
        resolved_port: list[str] = []

        def _open() -> tuple[str, Any]:
            port = self._port or find_face_port()
            if port is None:
                raise FaceDisplayError(
                    "No face display found (no USB device with vendor id "
                    f"{DISPLAY_USB_VID:#06x}). Pass port=... explicitly, or check the cable."
                )
            resolved_port.append(port)
            try:
                ser = factory(port, BAUDRATE, timeout=self._timeout)
            except Exception as exc:  # re-raised as our error type
                raise FaceDisplayError(f"Could not open face display on {port!r}: {exc}") from exc
            return port, ser

        def _on_late_open(opened: tuple[str, Any]) -> None:
            # Connect finished after the timeout already raised: nobody owns
            # this serial object, so close it instead of leaking an open port.
            late_port, ser = opened
            logger.warning(
                "Face display connect on %r finished after its %.1fs timeout had already fired; "
                "closing the orphaned, late-arriving serial port.",
                late_port,
                self._connect_timeout,
            )
            with contextlib.suppress(Exception):
                ser.close()

        try:
            port, self._ser = run_with_timeout(_open, timeout=self._connect_timeout, on_late_result=_on_late_open)
        except ProbeTimeoutError as exc:
            port_desc = resolved_port[0] if resolved_port else (self._port or "auto-detected face-display port")
            raise FaceDisplayConnectTimeoutError(
                f"Timed out connecting to the face display on {port_desc!r} after "
                f"{self._connect_timeout:.1f}s (no response opening the serial port). Check that "
                "the face display is powered and this is the correct port."
            ) from exc
        self._port = port

        if self._on_power_event is not None:
            self._stop.clear()
            self._reader = threading.Thread(target=self._read_loop, name="face-evt", daemon=True)
            self._reader.start()

    def disconnect(self) -> None:
        """Stop the reader thread (if any) and close the port."""
        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=self._timeout + 0.5)
            self._reader = None
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def __enter__(self) -> FaceDisplay:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()

    # ---- commands (one per firmware verb) -----------------------------

    def set_expression(self, name: str, hold_ms: int = 0) -> str:
        """Show an expression. ``hold_ms > 0`` auto-returns to IDLE after that.

        *name* is case-insensitive; firmware aliases (e.g. ``grin``, ``zzz``)
        are accepted and resolved on-device. ``hold_ms=0`` (default) holds the
        expression until the next command. Returns the firmware reply line.
        """
        arg = name.strip().upper()
        return self._command(f"EXPR {arg} {hold_ms}" if hold_ms > 0 else f"EXPR {arg}")

    def idle(self) -> str:
        """Return to the yellow IDLE (neutral) waiting face."""
        return self._command("IDLE")

    def wake(self) -> str:
        """Signal host-boot-complete: 💡IDEA for 3s, then rise to yellow IDLE."""
        return self._command("WAKE")

    def power(self, on: bool) -> str:
        """Sync power state: ``on=False`` → 💤SLEEP face; off→on triggers the 💡wake-up."""
        return self._command("POWER ON" if on else "POWER OFF")

    def set_brightness(self, percent: int) -> str:
        """Set backlight brightness (0–100, clamped on-device)."""
        return self._command(f"BL {int(percent)}")

    def list_expressions(self) -> str:
        """Ask the firmware for its expression vocabulary (the ``LIST`` reply)."""
        return self._command("LIST")

    def ping(self) -> bool:
        """Liveness check — ``True`` when the display answers ``PONG``."""
        return self._command("PING").strip().upper() == "PONG"

    # ---- transport ----------------------------------------------------

    def _command(self, line: str) -> str:
        """Send one command line and return the firmware's reply (stripped).

        ``_io_lock`` serializes command writes (and the reply wait that follows),
        so concurrent callers can't grab each other's replies. When the ``EVT``
        reader thread is running it owns the port's *reads*, so we wait for the
        reply on :attr:`_reply_queue`; otherwise we read it inline. Returns
        ``""`` if no reply arrives within *timeout* — the same as a serial read
        timeout — so liveness checks like :meth:`ping` stay boolean either way.
        """
        if self._ser is None:
            raise FaceDisplayError("Not connected. Call connect() before sending commands.")
        payload = (line.rstrip("\r\n") + "\n").encode("ascii", "ignore")
        with self._io_lock:
            try:
                if self._reader is not None:
                    # Drop any unsolicited line so we match THIS command's reply
                    # (we're the only consumer, so draining under the lock is safe).
                    while not self._reply_queue.empty():
                        self._reply_queue.get_nowait()
                    self._ser.write(payload)
                    try:
                        return self._reply_queue.get(timeout=self._timeout).strip()
                    except queue.Empty:
                        return ""
                self._ser.write(payload)
                reply = self._ser.readline()
            except Exception as exc:  # re-raised as our error type
                raise FaceDisplayError(f"Serial I/O failed for {line!r}: {exc}") from exc
        return reply.decode("ascii", "ignore").strip()

    def _read_loop(self) -> None:
        """Background reader (runs only when an ``on_power_event`` callback is set).

        Owns every read on the port: dispatches ``EVT POWER ON|OFF`` to the
        callback and routes other lines (command replies / ``LIST`` output) to
        :attr:`_reply_queue` for :meth:`_command`. Reads run *without* holding
        ``_io_lock`` — pyserial permits a concurrent read and write — so a
        blocking ``readline`` never stalls a command write.

        A read error (USB unplugged, device reset) ends the thread *and* clears
        the port so :attr:`is_connected` flips to ``False`` — the SDK does not
        auto-reconnect, so a caller watching ``is_connected`` can re-``connect``.
        """
        while not self._stop.is_set():
            ser = self._ser
            if ser is None:  # mid-shutdown: wait for _stop, don't busy-spin
                self._stop.wait(0.1)
                continue
            try:
                raw = ser.readline()
            except Exception as exc:  # USB yank etc.: tear the connection down so
                # is_connected reports False — callers can detect the drop and
                # decide whether to reconnect (the SDK doesn't auto-retry).
                logger.debug("face read loop stopped: %s", exc)
                self._ser = None
                with contextlib.suppress(Exception):
                    ser.close()
                return
            line = raw.decode("ascii", "ignore").strip()
            if not line:
                continue  # idle timeout — loop and re-check _stop
            upper = line.upper()
            if upper.startswith("EVT POWER "):
                arg = line[len("EVT POWER ") :].strip().upper()
                if arg in ("ON", "OFF") and self._on_power_event is not None:
                    try:
                        self._on_power_event(arg == "ON")
                    except Exception:  # never let a callback kill the reader
                        logger.exception("on_power_event callback raised")
            elif upper.startswith(("EVT ", "DBG")):
                logger.debug("face: %s", line)  # other events / debug noise — not a reply
            else:
                self._reply_queue.put(line)  # command reply, handed to _command()
