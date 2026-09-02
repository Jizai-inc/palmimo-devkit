# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""CLI: ``uv run python -m palmimo_sdk.mcp`` -- wire a real Palmimo to an MCP server.

Builds a real :class:`~palmimo_sdk.robot.Palmimo`, attaching whichever
peripherals can actually be reached (servo bus, face display, speaker, head
camera) and degrading to ``None`` for the rest, then serves
:func:`~palmimo_sdk.mcp.build_mcp_server`'s tool set over stdio (the default,
for a local MCP client such as Claude Code) or streamable HTTP (for a client
running on a different machine than the robot).

Every peripheral is probed the same way :func:`_build_servo_driver` probes
the servo bus (mirroring
``palmimo_wakeword_agent.wiring._build_servo_driver``): construct, then try to
connect/open it right away, catching failure (missing optional dependency,
no hardware attached, wrong port, ...) and warning instead of raising, so the
server still starts compute-only / voice-less / camera-less rather than
refusing to run at all. Warnings go to **stderr** -- stdio transport reserves
stdout for the MCP protocol stream itself, so anything printed to stdout
there would corrupt it. Each ``--no-*`` flag skips probing (and therefore
opening) the matching peripheral outright -- e.g. so a developer running this
on a PC does not have the display/speaker/camera probes grab a webcam or the
default audio device that happens to be attached.

:func:`main` validates ``--include``/``--exclude`` tool names *before*
probing any peripheral: :class:`~palmimo_sdk.agent.toolset.AgentToolSet`
itself only raises ``ValueError`` for an unknown name once it is constructed,
which is after every peripheral has already been probed (and, for the ones
that succeeded, opened) -- a typo there would otherwise leave hardware open
with nothing left to close it. Peripheral construction through serving then
runs inside one ``try``/``finally``, so whatever a probe opened is parked and
closed whether serving ends normally, is interrupted, or an unexpected
exception propagates -- with
:func:`_terminating_signals_raise_keyboard_interrupt` making a termination
signal reach that ``finally`` as an exception at all, and
:func:`_shutdown_signals_ignored` keeping the park from being cut short by
the next one. A hard kill (SIGKILL) or power loss runs no code here and still
leaves the servos energised.

Shutdown is on a clock, because the client that started it escalates to that
hard kill: see :data:`_SHUTDOWN_BUDGET_SECONDS`. Nothing here shortens the
shutdown to fit -- the park is deliberately uninterruptible -- so the clock is
used only to report, after the fact, that a stop overran the budget and would
therefore have been killed part-way through the park by a client that was
counting.

Tool calls are recorded too, one ``INFO`` line each (see
:func:`~palmimo_sdk.mcp.server._log_tool_call` for the record and
``--log-tool-calls`` for the detail levels). :func:`_tool_call_logging` is what
puts those records on **stderr** and keeps them off stdout, which on the stdio
transport carries the MCP protocol itself.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import logging
import os
import signal
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, NoReturn

import anyio
import anyio.to_thread
import mcp.server.stdio
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse

from palmimo_sdk import FaceDisplay, HeadCamera, Palmimo, PortDetectionError, Speaker, SpeakerConfig
from palmimo_sdk._signals import signals_ignored
from palmimo_sdk.agent import AgentToolSet
from palmimo_sdk.agent.toolset import TOOL_MODELS

from .server import TOOL_CALL_LOG_DETAILS, build_mcp_server


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import FrameType

    from starlette.types import ASGIApp, Receive, Scope, Send

    from palmimo_sdk import ServoDriver


# Hosts streamable_http_app() (mcp.server.lowlevel.Server) auto-enables its own
# DNS-rebinding protection for -- see the module docstring on _build_http_app
# for why a non-loopback bind needs a different treatment entirely.
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

_LOG_TOOL_CALLS_ENV_VAR = "PALMIMO_MCP_LOG_TOOL_CALLS"
_DEFAULT_LOG_TOOL_CALLS = "summary"

# The microphone array the kit ships with, which is also the loudspeaker's
# card. Named here rather than left to ALSA because a card *index* belongs to
# one boot's enumeration order: attach the array after the kernel has listed
# the built-in HDMI outputs and it lands behind them, leaving the default
# pointing at a device with no speaker on it. --speaker-device overrides it,
# and an empty value asks for the ALSA default explicitly.
_DEFAULT_SPEAKER_DEVICE = "ReSpeaker"

# The logger _tool_call_logging() configures: the whole `palmimo_sdk.mcp`
# subtree rather than just `palmimo_sdk.mcp.server`, so a record added
# elsewhere in this package lands on the same stderr stream instead of
# silently having nowhere to go.
_MCP_LOGGER_NAME = "palmimo_sdk.mcp"


def _split_names(csv: str | None) -> list[str] | None:
    """Split a comma-separated ``--include``/``--exclude`` value into tool names.

    ``None`` (the flag was not given) stays ``None`` -- distinct from an empty
    list, which would mean "include/exclude nothing" -- so
    :class:`~palmimo_sdk.agent.toolset.AgentToolSet` only narrows/excludes when
    the caller actually asked for it. Blank entries from stray commas/spaces
    (``"a,, b"``) are dropped rather than passed through as an invalid
    ``""`` tool name.
    """
    if csv is None:
        return None
    return [name.strip() for name in csv.split(",") if name.strip()]


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI's :class:`argparse.ArgumentParser`.

    Split out from :func:`_parse_args` so :func:`main` can also reach
    :meth:`~argparse.ArgumentParser.error` for the ``--include``/``--exclude``
    name validation (see the module docstring) -- ``parser.error()`` prints a
    friendly usage message and exits the same way a normal argparse failure
    would, rather than main() raising a bare ``ValueError`` after hardware is
    already open.
    """
    parser = argparse.ArgumentParser(
        prog="python -m palmimo_sdk.mcp",
        description="Serve a Palmimo robot's agent tools over the Model Context Protocol.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="MCP transport: 'stdio' for a local client (e.g. Claude Code) sharing this process's "
        "stdin/stdout, or 'http' (streamable HTTP) for a client on a different machine than the "
        "robot. Default: stdio.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind for --transport http. WARNING: without a token this server has no "
        "authentication -- only bind 0.0.0.0 (or any non-loopback address) on a network you trust, "
        "or set PALMIMO_MCP_TOKEN (preferred) / pass --token to require a Bearer token on every "
        "request. Default: 127.0.0.1.",
    )
    parser.add_argument("--port", type=int, default=8765, help="Port to bind for --transport http. Default: 8765.")
    parser.add_argument(
        "--token",
        default=os.environ.get("PALMIMO_MCP_TOKEN"),
        help="Require 'Authorization: Bearer <token>' on every request, for --transport http only. "
        "Reads the PALMIMO_MCP_TOKEN environment variable by default; an explicit --token overrides "
        "it. Prefer the environment variable over --token: a value passed on the command line is "
        "visible to any other local user via `ps`. Default: no authentication (unchanged behavior).",
    )
    parser.add_argument(
        "--servo-port",
        default=None,
        help="Explicit servo bus serial port (e.g. /dev/ttyUSB0). Default: auto-detect.",
    )
    parser.add_argument(
        "--no-servo",
        action="store_true",
        help="Do not attach a servo driver -- motions run compute-only (no hardware movement).",
    )
    parser.add_argument(
        "--no-display",
        action="store_true",
        help="Do not attach the face display -- set_face/show_emoji report no display attached.",
    )
    parser.add_argument(
        "--no-speaker",
        action="store_true",
        help="Do not attach the speaker -- say reports no speaker attached.",
    )
    parser.add_argument(
        "--no-camera",
        action="store_true",
        help="Do not attach the head camera -- capture reports no camera attached.",
    )
    parser.add_argument(
        "--speaker-device",
        default=_DEFAULT_SPEAKER_DEVICE,
        help="Substring naming the ALSA playback card for say, matched against the card id or its "
        "long name -- never a card index, which changes when a USB audio device is replugged. "
        f"Default: {_DEFAULT_SPEAKER_DEVICE}. Pass an empty string to use whatever ALSA considers "
        "default; an unmatched name falls back to the same thing with a warning.",
    )
    parser.add_argument(
        "--include",
        default=None,
        help="Comma-separated tool names to expose exclusively (all others are hidden). "
        "Composes with --exclude: --include narrows first, --exclude then removes from that.",
    )
    parser.add_argument(
        "--exclude",
        default=None,
        help="Comma-separated tool names to hide from the exposed set.",
    )
    parser.add_argument(
        "--log-tool-calls",
        choices=TOOL_CALL_LOG_DETAILS,
        default=os.environ.get(_LOG_TOOL_CALLS_ENV_VAR, _DEFAULT_LOG_TOOL_CALLS),
        help="How much of each tool call to log to stderr, one INFO line per call: 'summary' (the "
        "default) logs the tool name, its argument names with any numeric values, the outcome "
        "(ok/error/interrupted), how long the call took, and the size of the result; 'full' also logs "
        "every argument value and the result text verbatim; 'off' logs nothing per call. Reads the "
        f"{_LOG_TOOL_CALLS_ENV_VAR} environment variable by default; an explicit --log-tool-calls "
        "overrides it. Records always go to stderr -- on the stdio transport stdout carries the MCP "
        "protocol itself.",
    )
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for ``python -m palmimo_sdk.mcp``."""
    return _build_arg_parser().parse_args(argv)


def _validate_tool_names(parser: argparse.ArgumentParser, flag: str, names: list[str] | None) -> None:
    """Fail fast via ``parser.error()`` when *names* contains a name not in :data:`TOOL_MODELS`.

    Mirrors :class:`~palmimo_sdk.agent.toolset.AgentToolSet`'s own
    include=/exclude= validation message shape, but runs before any
    peripheral is probed -- see the module docstring for why that ordering
    matters. ``parser.error()`` prints the parser's usage plus the message to
    stderr and raises ``SystemExit(2)``, matching how every other CLI
    argument mistake is reported here.
    """
    if names is None:
        return
    unknown = set(names) - TOOL_MODELS.keys()
    if unknown:
        parser.error(f"Unknown tool name(s) in {flag}: {sorted(unknown)}. Available: {sorted(TOOL_MODELS)}")


def _validate_token_transport(parser: argparse.ArgumentParser, token: str | None, transport: str) -> None:
    """Fail fast via ``parser.error()`` when ``--token`` is combined with ``--transport stdio``.

    stdio has no request to attach an ``Authorization`` header to -- there is
    no framework layer there for a bearer check to hook into -- so requiring
    one is a user mistake to catch up front, before any peripheral is probed
    (mirroring :func:`_validate_tool_names`), rather than a token that is
    silently ignored.
    """
    if token is not None and transport == "stdio":
        parser.error("--token is only valid with --transport http (stdio has no request to authenticate)")


def _validate_log_tool_calls(parser: argparse.ArgumentParser, detail: str) -> None:
    """Fail fast via ``parser.error()`` when the log detail is not one of :data:`TOOL_CALL_LOG_DETAILS`.

    ``argparse``'s ``choices=`` does not cover this on its own: a default that
    is never overridden on the command line is type-converted but not
    choice-checked, so a typo in the ``PALMIMO_MCP_LOG_TOOL_CALLS``
    environment variable would otherwise reach ``build_mcp_server`` unnoticed
    and be treated as "not full, not off" -- i.e. silently behave like the
    default it was trying to change. Checking it here reports the typo the
    same way every other CLI mistake in this module is reported, and before
    any peripheral is probed (mirroring :func:`_validate_tool_names`).
    """
    if detail not in TOOL_CALL_LOG_DETAILS:
        parser.error(
            f"Invalid tool-call log detail {detail!r} (from --log-tool-calls or "
            f"{_LOG_TOOL_CALLS_ENV_VAR}). Choose from: {', '.join(TOOL_CALL_LOG_DETAILS)}"
        )


@contextlib.contextmanager
def _tool_call_logging(detail: str) -> Iterator[None]:
    """Route this package's log records to stderr for the duration of the block.

    stdout is deliberately not an option, and not merely by choosing a stream:
    the handler installed here goes on the ``palmimo_sdk.mcp`` logger with
    ``propagate`` turned **off**, so a record cannot also travel up to a root
    handler that some other component pointed at stdout. On the stdio
    transport stdout is the MCP protocol stream, and a single stray byte on it
    desynchronises the session -- so "these records never reach stdout" has to
    hold regardless of what else in the process has configured logging, not
    just when nothing else has.

    The cost of that is stated plainly: an application that wraps this CLI and
    collects logs through the root logger will not see these records there.
    That is the right way round for a program whose stdout is a protocol
    channel; an application that wants them elsewhere can add its own handler
    to ``palmimo_sdk.mcp`` and run with ``--log-tool-calls off``, or call
    :func:`~palmimo_sdk.mcp.build_mcp_server` directly.

    Nothing outside this package's own logger is touched -- no
    :func:`logging.basicConfig`, no root-logger level -- so importing and
    calling :func:`main` does not quietly turn logging on for everything else
    in the process. The previous level, propagation, and handler set are
    restored on the way out for the same reason
    :func:`_terminating_signals_raise_keyboard_interrupt` restores signal
    handlers: ``main()`` is an ordinary callable that the test suite runs
    in-process, and it must not leave process-wide state behind it.

    ``off`` installs nothing at all, rather than installing a handler and
    filtering afterwards, so an application that has configured its own
    handler for this logger keeps it and sees whatever it asked for.
    """
    if detail == "off":
        yield
        return
    mcp_logger = logging.getLogger(_MCP_LOGGER_NAME)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    previous_level = mcp_logger.level
    previous_propagate = mcp_logger.propagate
    mcp_logger.addHandler(handler)
    mcp_logger.setLevel(logging.INFO)
    mcp_logger.propagate = False
    try:
        yield
    finally:
        mcp_logger.removeHandler(handler)
        handler.close()
        mcp_logger.setLevel(previous_level)
        mcp_logger.propagate = previous_propagate


def _close_interrupted_probe(close: Callable[[], None], label: str) -> None:
    """Close a peripheral whose probe was cut short by a shutdown signal.

    A probe *opens* what it returns, so an interrupt raised inside the open
    leaves the resource live while the assignment in :func:`main` never
    happens -- nothing else holds a reference, and nothing else can close it.
    The servo bus is the one that matters: its connect enables torque on every
    servo before returning.

    Only for a ``BaseException``. An ordinary ``Exception`` means "this
    peripheral is not available", which the probes deliberately degrade to
    ``None`` for; the resource is unusable, not orphaned.
    """
    print(f"shutdown during {label} probe -- closing it", file=sys.stderr)
    with contextlib.suppress(Exception):
        close()


def _build_servo_driver(servo_port: str | None) -> ServoDriver | None:
    """Build and probe-connect the servo driver, degrading to compute-only on failure.

    Same pattern as ``palmimo_wakeword_agent.wiring._build_servo_driver``:
    :class:`~palmimo_sdk.io.dynamixel.DynamixelDriver` never touches hardware
    in ``__init__``, so the only way to probe it is to call :meth:`connect`
    right away and see whether it raises.
    """
    from palmimo_sdk import DynamixelDriver

    driver = DynamixelDriver(port=servo_port)
    try:
        driver.connect()
    except PortDetectionError as exc:
        print(f"servo bus not available -- motions run compute-only ({exc})", file=sys.stderr)
        return None
    except Exception as exc:  # missing `hardware` extra, serial-layer error, etc.
        print(f"servo bus not available -- motions run compute-only ({exc})", file=sys.stderr)
        return None
    except BaseException:
        _close_interrupted_probe(driver.disconnect, "servo bus")
        raise
    return driver


def _build_display() -> FaceDisplay | None:
    """Build and probe-connect the face display, degrading to ``None`` on failure."""
    display = FaceDisplay()
    try:
        display.connect()
    except Exception as exc:  # no display attached, missing `face` extra (pyserial), etc.
        print(f"face display not available -- expressions disabled ({exc})", file=sys.stderr)
        return None
    except BaseException:
        _close_interrupted_probe(display.disconnect, "face display")
        raise
    return display


def _build_speaker(device_name_hint: str | None = None) -> Speaker | None:
    """Build and probe-open the speaker (a piper availability check), degrading to ``None`` on failure.

    *device_name_hint* names the ALSA playback card by a substring of its id
    rather than letting the card index ALSA happens to assign decide, so the
    server keeps speaking through the same output across a USB replug. Falsy
    takes ALSA's default, as it does everywhere else.
    """
    speaker = Speaker(SpeakerConfig(device_name_hint=device_name_hint))
    try:
        speaker.open()
    except Exception as exc:  # piper missing, missing `speech` extra, etc.
        print(f"speaker not available -- speech disabled ({exc})", file=sys.stderr)
        return None
    except BaseException:
        _close_interrupted_probe(speaker.close, "speaker")
        raise
    return speaker


def _build_camera() -> HeadCamera | None:
    """Build and probe-open the head camera, degrading to ``None`` on failure."""
    camera = HeadCamera()
    try:
        camera.open()
    except Exception as exc:  # no camera attached, missing `vision` extra (opencv), etc.
        print(f"head camera not available -- capture disabled ({exc})", file=sys.stderr)
        return None
    except BaseException:
        _close_interrupted_probe(camera.close, "head camera")
        raise
    return camera


async def _serve_stdio(server: Server[Any]) -> None:
    """Run *server* over stdio until the client disconnects."""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


# How long this process has, from the first terminating signal, before an MCP
# stdio client stops waiting. The client escalates on a fixed schedule: close
# stdin at t=0, SIGTERM at t=2.0s (``PROCESS_TERMINATION_TIMEOUT``), SIGKILL at
# t=4.0s (a further ``FORCE_KILL_TIMEOUT``), both in ``mcp.client.stdio``.
# SIGKILL cannot be caught and runs no code, so a shutdown that exceeds this is
# killed part-way through the park and leaves the servos energised. Nothing
# enforces the bound in code -- the park is deliberately uninterruptible, and
# cutting it short would defeat its purpose -- so overrunning it is reported on
# stderr instead, where a hardware run sees it.
_SHUTDOWN_BUDGET_SECONDS = 4.0


class _ShutdownRequest:
    """Whether a terminating signal has arrived, and how long ago.

    The clock started by :meth:`record` is what :func:`_park_and_close`
    measures the shutdown against :data:`_SHUTDOWN_BUDGET_SECONDS` with.
    """

    def __init__(self) -> None:
        self._at: float | None = None

    def record(self) -> None:
        """Note the first terminating signal; a later one does not move the clock."""
        if self._at is None:
            self._at = time.monotonic()

    def seconds_since(self) -> float | None:
        """Seconds since the first terminating signal, or ``None`` if none arrived."""
        return None if self._at is None else time.monotonic() - self._at


# SIGINT is deliberately absent: Python already delivers it as a
# KeyboardInterrupt, which is the exact behavior the handler below installs for
# the rest. SIGHUP is included because losing the controlling terminal (closing
# the window, or an ssh session dropping) is a routine way for this server to
# end -- the robot is normally driven over ssh -- and its default disposition
# kills the process just as silently as SIGTERM's. It is looked up by name
# because Windows has no SIGHUP.
_TERMINATING_SIGNALS: tuple[signal.Signals, ...] = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)
)


@contextlib.contextmanager
def _terminating_signals_raise_keyboard_interrupt() -> Iterator[_ShutdownRequest]:
    """Make :data:`_TERMINATING_SIGNALS` raise instead of killing the process outright.

    Without this the robot is left torque-enabled whenever the server is asked
    to stop by signal rather than by Ctrl-C: Python's default disposition for
    SIGTERM/SIGHUP terminates the interpreter without unwinding, so no
    ``finally`` runs and nothing ever parks. That is the *common* path, not an
    edge case -- an MCP client shutting down its stdio server sends SIGTERM,
    as does ``systemd`` and a plain ``kill`` -- and a robot whose neck servos
    stay energised holds the head up against gravity and heats fast.

    Raising ``KeyboardInterrupt`` reuses the unwind Ctrl-C already gets *in
    this CLI* -- the caller's ``finally`` -- rather than adding a second
    shutdown route to keep in step with it. It is deliberately NOT equivalent
    to what SIGINT does inside the stdio event loop: asyncio installs a
    cooperative cancellation for SIGINT, which lets
    :mod:`palmimo_sdk.mcp.server`'s shield loop drain an in-flight tool call,
    whereas an exception raised from a handler unwinds at whatever bytecode
    happened to be executing. Draining an in-flight call on a termination
    signal would need that cooperative path (an anyio signal receiver
    cancelling the serving scope) and is not attempted here.

    The yielded :class:`_ShutdownRequest` timestamps the first such signal, so
    :func:`_park_and_close` can measure the whole shutdown against
    :data:`_SHUTDOWN_BUDGET_SECONDS`.

    This also fixes the HTTP transport, where uvicorn is *not* the safety net
    it looks like. uvicorn does handle SIGTERM gracefully, but
    ``uvicorn.Server.capture_signals`` then restores the previous handler and
    re-raises the signal at itself so the caller observes the platform's usual
    outcome -- which, with the default disposition in place, kills the process
    from inside ``uvicorn.run()`` (exit code 143) before it can return. What it
    restores and re-raises into is whatever was installed underneath it, so
    installing this handler *around* uvicorn is what turns that re-raise into a
    normal unwind.

    The previous handlers are restored on the way out, since ``main()`` is an
    ordinary callable (the test suite calls it in-process) and must not leave a
    process-wide disposition changed behind it. The park itself runs under
    :func:`_shutdown_signals_ignored`, so the signal that started a shutdown
    cannot also cut that shutdown short.

    Off the main thread :func:`signal.signal` is not allowed, so nothing is
    installed and this degrades to the platform default -- i.e. back to the bug
    above, with a termination signal killing the process torque-enabled. The
    process still *receives* the signal there; only this code's ability to act
    on it is missing, so the degradation is announced on stderr rather than
    passed off as supported.
    """
    shutdown = _ShutdownRequest()

    def raise_keyboard_interrupt(signum: int, frame: FrameType | None) -> NoReturn:
        # Timestamped before raising, so the budget is measured from the signal
        # itself rather than from wherever the unwind happens to land.
        shutdown.record()
        raise KeyboardInterrupt

    if threading.current_thread() is not threading.main_thread():
        print(
            "WARNING: not running on the main thread -- cannot install shutdown signal handlers. "
            "A termination signal (SIGTERM/SIGHUP) will kill this process without releasing servo "
            "torque; the servos will stay energised and heat up.",
            file=sys.stderr,
        )
        yield shutdown
        return
    previous = {sig: signal.signal(sig, raise_keyboard_interrupt) for sig in _TERMINATING_SIGNALS}
    try:
        yield shutdown
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


# SIGINT joins the terminating signals here, unlike in the raise-set above:
# once the park is running, Ctrl-C is one more way to skip the torque-off, and
# an operator mashing it while the robot eases down is the likeliest source of
# a repeat signal there. Palmimo.disconnect() now absorbs a KeyboardInterrupt
# around its whole body -- the leg return, the neck release, every peripheral
# close, and the driver disconnect that cuts torque -- for exactly that reason.
_PARK_UNINTERRUPTIBLE_SIGNALS: tuple[signal.Signals, ...] = (*_TERMINATING_SIGNALS, signal.SIGINT)


@contextlib.contextmanager
def _shutdown_signals_ignored() -> Iterator[None]:
    """Make :data:`_PARK_UNINTERRUPTIBLE_SIGNALS` no-ops for the duration of the block.

    Wraps the park, which is the one stretch of this program that must run to
    completion: it is what actually de-energises the servos, and the call that
    cuts torque (``ServoDriver.disconnect``) is the *last* statement in
    :meth:`~palmimo_sdk.robot.Palmimo.disconnect`. Everything before it --
    the neck-release ramp, the peripheral closes -- is guarded against
    ``Exception`` but not against ``BaseException``, so a ``KeyboardInterrupt``
    landing mid-park escapes and skips the torque-off entirely.

    That is not hypothetical: an MCP stdio client shuts a server down by
    closing stdin, waiting ``PROCESS_TERMINATION_TIMEOUT`` (2.0s), then sending
    SIGTERM. The park is a ~2.5s neck ramp plus the leg return, so on an
    ordinary client disconnect the SIGTERM lands *during* the park, every time.

    ``SIG_IGN`` rather than :func:`signal.pthread_sigmask`: a blocked signal
    stays pending and fires the moment it is unblocked, which would just move
    the kill to immediately after the park; an ignored one is discarded. The
    park is bounded, and a caller that genuinely must stop it still has
    SIGKILL -- which this cannot (and should not) intercept.

    A thin wrapper over :func:`~palmimo_sdk._signals.signals_ignored`, which
    also backs :func:`~palmimo_sdk.robot._deferred_interrupts`.
    """
    with signals_ignored(*_PARK_UNINTERRUPTIBLE_SIGNALS):
        yield


def _park_and_close(robot: Palmimo, shutdown: _ShutdownRequest) -> None:
    """Park *robot* and close its peripherals, uninterruptibly, reporting on stderr.

    The start/finish lines are the only evidence a hardware run gets that the
    torque-off actually completed: without them a park cut short by an
    exception is indistinguishable from a clean shutdown, since the caller
    suppresses the interrupt and exits 0 either way. The completion line is
    deliberately after the call, so it cannot report a partial park as success.

    A signal-initiated shutdown is then measured against
    :data:`_SHUTDOWN_BUDGET_SECONDS`, and an overrun is reported. Nothing is
    shortened to fit that budget, so the report is the only way an operator
    learns that this particular stop was long enough for a client counting down
    to SIGKILL to have killed the process mid-park -- i.e. that torque may have
    been left enabled -- rather than rediscovering it on a hot servo. Overrun
    is not an error in this process, which finished parking either way.
    """
    parking = robot.is_connected
    if parking:
        print("parking the robot -- easing to neutral and releasing servo torque...", file=sys.stderr)
    with _shutdown_signals_ignored():
        robot.disconnect(park=True)
    if parking:
        print("park complete -- servo torque released", file=sys.stderr)
    elapsed = shutdown.seconds_since()
    if elapsed is not None and elapsed > _SHUTDOWN_BUDGET_SECONDS:
        print(
            f"WARNING: shutdown took {elapsed:.2f}s, over the {_SHUTDOWN_BUDGET_SECONDS:.1f}s an MCP client "
            "allows before SIGKILL -- a client-driven stop this slow is killed mid-park and leaves the "
            "servos energised.",
            file=sys.stderr,
        )


class _RejectBrowserOriginASGIApp:
    """ASGI middleware rejecting any HTTP request that carries an ``Origin`` header.

    See :func:`_build_http_app` for why this exists instead of relying on
    ``streamable_http_app``'s built-in ``TransportSecuritySettings`` host
    check for a non-loopback bind.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and any(name == b"origin" for name, _value in scope["headers"]):
            response = PlainTextResponse("Origin header not accepted on this transport", status_code=403)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


class _BearerTokenASGIApp:
    """ASGI middleware requiring ``Authorization: Bearer <token>`` matching a static *token*.

    Hand-rolled rather than wired through ``Server.streamable_http_app``'s own
    ``token_verifier=``/``auth=`` parameters (see :func:`_build_http_app`'s
    docstring for why): those only take effect when an ``auth=AuthSettings``
    is also supplied, and ``AuthSettings`` requires ``issuer_url`` and
    ``resource_server_url`` -- a full OAuth resource-server configuration --
    which is far more than this CLI's single static token needs. This class
    follows the same pattern as :class:`_RejectBrowserOriginASGIApp`: a plain
    ASGI callable wrapping the inner app, added via ``Starlette.add_middleware``.

    A missing or mismatched token gets a 401 with ``WWW-Authenticate:
    Bearer``, per RFC 6750. The auth-scheme token (``Bearer``) is matched
    case-insensitively, also per RFC 6750 (``bearer``/``BEARER``/``Bearer``
    are all valid). Comparison uses :func:`hmac.compare_digest` on the raw
    header *bytes* -- never decoded to ``str`` -- both because the configured
    token is compared against exactly what the client sent (no decode step
    that could itself raise) and because ``compare_digest`` rejects a ``str``
    argument containing non-ASCII characters outright (``TypeError``), which
    a decoded presented token could otherwise trigger; a wrong guess cannot
    be timed to learn the correct token one byte at a time either way.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        # Encoded once, up front, so every request compares bytes to bytes --
        # see the class docstring for why.
        self._token = token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        header_values = [value for name, value in scope["headers"] if name == b"authorization"]
        # More than one Authorization header is ambiguous -- which one a
        # proxy in front of this server meant is not this server's call to
        # make -- so reject rather than picking one.
        if len(header_values) != 1:
            await self._reject(scope, receive, send)
            return
        scheme, _separator, presented = header_values[0].partition(b" ")
        if scheme.lower() != b"bearer" or not presented or not hmac.compare_digest(presented, self._token):
            await self._reject(scope, receive, send)
            return
        await self._app(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = PlainTextResponse("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"})
        await response(scope, receive, send)


def _build_http_app(server: Server[Any], host: str = "127.0.0.1", token: str | None = None) -> Starlette:
    """Build the streamable-HTTP ASGI app serving *server* at ``/mcp``.

    Split out from :func:`main` so it can be exercised without actually
    binding a socket (uvicorn). Delegates to
    :meth:`~mcp.server.lowlevel.Server.streamable_http_app` (SDK 2.0), which
    replaces this module's former hand-rolled Starlette wiring: it registers
    the streamable-HTTP ASGI handler via ``Route("/mcp", endpoint=...)`` --
    not ``Mount(...)`` -- so a bare ``POST /mcp`` (no trailing slash) is
    served directly rather than 307-redirected to ``/mcp/`` (which would lose
    the request body on most clients), matching this CLI's own
    remote-registration example (``.../mcp``, no trailing slash).

    Security posture differs by *host* (see ``mcp.server.lowlevel.Server.streamable_http_app``
    and ``mcp.server.transport_security.TransportSecurityMiddleware`` for the
    behavior this comment is describing):

    * For a loopback *host* (:data:`_LOOPBACK_HOSTS`), ``streamable_http_app``
      auto-enables its own ``TransportSecuritySettings`` DNS-rebinding
      protection (a Host- and Origin-header allowlist), which is left as-is.
    * For any other *host*, that auto-enable does NOT kick in, and there is
      no static ``allowed_hosts`` value that works there:
      ``TransportSecurityMiddleware._validate_host`` only ever accepts an
      exact ``Host`` value or a literal ``"<prefix>:*"`` port-wildcard
      prefix -- never a true wildcard -- so a static list would either reject
      every legitimate client (an 0.0.0.0 bind's Host header is whatever IP
      the client happened to dial, not known ahead of time) or would have to
      already be so broad it accepts everything. So the Host check is
      explicitly disabled here (``enable_dns_rebinding_protection=False``)
      rather than left silently off, and is replaced with a narrower,
      purpose-fit guard: :class:`_RejectBrowserOriginASGIApp` rejects any
      request carrying an ``Origin`` header at all. A non-browser MCP client
      (curl, the reference ``mcp`` client, this SDK's own examples) never
      sends one; only a browser's ``fetch``/``XHR`` does -- which is exactly
      the DNS-rebinding attack vector this exists to stop, without needing to
      guess what Host header a legitimate client will present.

    *token*, when given, adds :class:`_BearerTokenASGIApp` requiring
    ``Authorization: Bearer <token>`` on every request, independently of
    *host* -- unrelated to the Origin/DNS-rebinding posture above, which
    guards against a *browser* abusing a trusting client; the token instead
    authenticates the caller itself, for any *host*. ``Starlette.add_middleware``
    inserts each call at the front of the middleware stack, so when both this
    and the non-loopback Origin guard are active, the token check runs first
    (outermost) -- but a valid token does not bypass the Origin guard, which
    still runs afterwards, on the way in.
    """
    if host in _LOOPBACK_HOSTS:
        app = server.streamable_http_app(host=host)
    else:
        app = server.streamable_http_app(
            host=host,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        app.add_middleware(_RejectBrowserOriginASGIApp)
    if token is not None:
        app.add_middleware(_BearerTokenASGIApp, token=token)
    return app


def main(argv: list[str] | None = None) -> None:
    """Build the robot, attach an MCP server, and serve until interrupted."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    include = _split_names(args.include)
    exclude = _split_names(args.exclude)
    # Validated before any peripheral is probed -- see the module docstring.
    _validate_tool_names(parser, "--include", include)
    _validate_tool_names(parser, "--exclude", exclude)
    _validate_token_transport(parser, args.token, args.transport)
    _validate_log_tool_calls(parser, args.log_tool_calls)

    # The signal context opens BEFORE the first probe, not just around serving:
    # _build_servo_driver() connects the bus, which enables torque on all 21
    # servos, and the display/speaker/camera probes that follow routinely take
    # seconds on a Pi. A termination signal in that window would otherwise hit
    # the default disposition and kill the process with the servos already
    # energised. Suppressing KeyboardInterrupt is the OUTER context so that a
    # signal arriving while the handlers are being restored is still caught
    # rather than escaping main().
    #
    # Everything from the first probe through serving is one try/finally, so
    # whatever was built is parked and closed on a clean return, on any signal
    # _terminating_signals_raise_keyboard_interrupt covers, and on any other
    # exception. SIGKILL and power loss stay outside any such promise: neither
    # runs code here, so both still leave the servos torque-enabled.
    #
    # The tool-call log is configured inside the same nest, so it is torn down
    # with everything else rather than left on a module-global logger.
    with (
        contextlib.suppress(KeyboardInterrupt),
        _tool_call_logging(args.log_tool_calls),
        _terminating_signals_raise_keyboard_interrupt() as shutdown,
    ):
        driver: ServoDriver | None = None
        display: FaceDisplay | None = None
        speaker: Speaker | None = None
        camera: HeadCamera | None = None
        robot: Palmimo | None = None
        try:
            driver = None if args.no_servo else _build_servo_driver(args.servo_port)
            display = None if args.no_display else _build_display()
            speaker = None if args.no_speaker else _build_speaker(args.speaker_device)
            camera = None if args.no_camera else _build_camera()

            robot = Palmimo(driver=driver, display=display, speaker=speaker, camera=camera)
            toolset = AgentToolSet(robot, include=include, exclude=exclude)
            server = build_mcp_server(toolset, log_tool_calls=args.log_tool_calls)

            # robot.connect() raises if nothing is attached at all (a fully
            # compute-only run, e.g. every peripheral --no-*'d or unreachable)
            # -- so only connect when there is something to connect.
            if robot.has_connectable_resource:
                robot.connect()
            if args.transport == "stdio":
                anyio.run(_serve_stdio, server)
            else:
                if args.host not in _LOOPBACK_HOSTS and args.token is None:
                    print(
                        f"WARNING: binding {args.host}:{args.port} -- this server has no authentication; "
                        "only do this on a network you trust, or set PALMIMO_MCP_TOKEN (preferred) / pass "
                        "--token to enable Bearer authentication.",
                        file=sys.stderr,
                    )
                print(f"serving MCP over streamable HTTP at http://{args.host}:{args.port}/mcp", file=sys.stderr)
                uvicorn.run(
                    _build_http_app(server, host=args.host, token=args.token),
                    host=args.host,
                    port=args.port,
                    log_level="warning",
                )
        finally:
            # Rebuilt from the probes when serving was never reached: each
            # probe *opens* what it returns, so a signal landing between two of
            # them leaves real hardware open with no facade owning it yet.
            # disconnect() is idempotent and skips the park when not connected,
            # so this is also the right call for a driver that was only probed.
            if robot is None:
                robot = Palmimo(driver=driver, display=display, speaker=speaker, camera=camera)
            if robot.has_connectable_resource:
                _park_and_close(robot, shutdown)


if __name__ == "__main__":
    main()
