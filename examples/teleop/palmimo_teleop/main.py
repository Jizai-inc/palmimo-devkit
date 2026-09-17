"""`palmimo-teleop` CLI: build the robot, session, and video wiring, then serve the web app.

All the real wiring -- which driver, which camera -- lives in this module's
`_build_robot`/`_build_camera` helpers, kept close to the CLI options that
choose them; `server.create_app` only ever sees the already-built pieces.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import typer
import uvicorn

from palmimo_sdk import DynamixelDriver, HeadCamera, Palmimo, ServoDriver

from .robot_loop import RobotLoop
from .server import create_app
from .session import PilotSession
from .video import VideoStream


_LOG = logging.getLogger(__name__)

#: Default gait speed applied to every teleop-driven motion. There is no
#: per-frame speed scaling here (unlike the gamepad example's stick
#: deflection) -- buttons are on/off, so one fixed speed is the whole knob.
DEFAULT_GAIT_SPEED: float = 0.012

#: Avoids 8765 (the MCP server) and 80/8080 (Palmimo Portal).
DEFAULT_PORT: int = 8000

#: How many times the startup servo-bus probe is retried before degrading to
#: compute-only. A real incident: the probe took its one ping right after a
#: prior process was force-killed, and lost servo ID 19's reply to the
#: bus noise that left behind -- a manual scan moments later found all 21
#: servos responding. One probe attempt is not enough to tell "no bus" apart
#: from "bus momentarily noisy".
PROBE_RETRY_ATTEMPTS: int = 3

#: Gap between retry attempts.
PROBE_RETRY_INTERVAL_S: float = 1.0


def _probe_servo_driver(servo_port: str | None) -> Exception | None:
    """Attempt one connect/disconnect probe of a fresh `DynamixelDriver`.

    A fresh driver per attempt, since a failed `connect()` may leave one
    partially open. Mirrors the gamepad teleop example's
    `wiring._build_servo_driver`: a driver's `__init__` never touches
    hardware, so probing means calling `connect()` here explicitly and
    disconnecting again -- `Palmimo.connect()` (called later, from the
    server lifespan) does the connect that actually stays open for the run.

    Returns:
        Exception, optional: the exception raised by the failed attempt, or
            `None` on success.
    """
    driver = DynamixelDriver(port=servo_port)
    try:
        driver.connect()
        driver.disconnect()
    except Exception as exc:  # PortDetectionError, missing hardware extra, serial-layer error, etc.
        return exc
    return None


def _build_servo_driver(
    servo_port: str | None,
    *,
    probe: Callable[[str | None], Exception | None] = _probe_servo_driver,
    sleep: Callable[[float], None] = time.sleep,
) -> ServoDriver | None:
    """Build a `DynamixelDriver`, retrying the connect/disconnect probe before giving up.

    Retries up to `PROBE_RETRY_ATTEMPTS` times, `PROBE_RETRY_INTERVAL_S`
    apart, before degrading to compute-only -- see `PROBE_RETRY_ATTEMPTS`'s
    docstring for the incident this guards against. *probe* and *sleep* are
    injected so tests can drive the retry loop without real hardware or
    real time.
    """
    last_exc: Exception | None = None
    for attempt in range(1, PROBE_RETRY_ATTEMPTS + 1):
        last_exc = probe(servo_port)
        if last_exc is None:
            return DynamixelDriver(port=servo_port)
        _LOG.warning("servo bus probe attempt %d/%d failed: %s", attempt, PROBE_RETRY_ATTEMPTS, last_exc)
        if attempt < PROBE_RETRY_ATTEMPTS:
            sleep(PROBE_RETRY_INTERVAL_S)
    print(f"servo bus not available after {PROBE_RETRY_ATTEMPTS} attempts -- motions run compute-only ({last_exc})")
    return None


def _build_robot(*, dry_run: bool, gait_speed: float, fps: int, servo_port: str | None) -> Palmimo:
    """Build the `Palmimo` facade: compute-only under `--dry-run`, else a probed `DynamixelDriver`."""
    driver = None if dry_run else _build_servo_driver(servo_port)
    return Palmimo(gait_speed=gait_speed, fps=fps, driver=driver)


def _build_camera(no_camera: bool) -> HeadCamera | None:
    """Build a `HeadCamera` unless `--no-camera` was passed.

    Construction never touches hardware (see `palmimo_sdk.io.camera`), so
    this never fails on its own -- an unavailable camera is instead
    discovered and handled by `video.VideoStream.start()`, which is what lets
    it degrade to `camera_ok: false` rather than crash the whole server.
    """
    if no_camera:
        return None
    return HeadCamera()


app = typer.Typer(add_completion=False)


@app.command()
def main(
    port: int = typer.Option(DEFAULT_PORT, "--port", help="HTTP/WebSocket port to serve on."),
    fps: int = typer.Option(60, "--fps", help="Robot control loop rate in cycles per second.", min=1),
    gait_speed: float = typer.Option(
        DEFAULT_GAIT_SPEED, "--gait-speed", help="`Palmimo` gait speed.", min=0.001, max=0.05
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute-only: never attach a servo driver."),
    no_camera: bool = typer.Option(False, "--no-camera", help="Never attach the head camera."),
    servo_port: str | None = typer.Option(
        None, "--servo-port", help="Servo bus serial port, e.g. /dev/ttyACM0 (auto-detected if omitted)."
    ),
) -> None:
    """Serve the Palmimo web teleop app: a phone/laptop browser drives the robot and views its
    head camera, on the same LAN, with no authentication."""
    robot = _build_robot(dry_run=dry_run, gait_speed=gait_speed, fps=fps, servo_port=servo_port)
    session = PilotSession()
    robot_loop = RobotLoop(robot, session, fps=fps)
    video = VideoStream(_build_camera(no_camera))
    app_instance = create_app(robot, session, robot_loop, video)

    mode = (
        "--dry-run"
        if dry_run
        else ("compute-only (no servo bus found)" if not robot.has_connectable_resource else "live")
    )
    print(f"Palmimo teleop starting on http://0.0.0.0:{port} ({mode}). Open /pilot to drive, / to view.", flush=True)
    # Binds every interface deliberately: a phone on the same LAN reaches this
    # by the robot's Wi-Fi IP, not 127.0.0.1. See the README's Safety notes --
    # there is no authentication, so this is LAN-only by convention, not by code.
    # ws_ping_interval/timeout shorten how long a silently-dropped pilot
    # WebSocket (Wi-Fi walks out of range, laptop sleeps) takes to actually
    # disconnect server-side and free the pilot slot -- without them uvicorn's
    # default ping cadence leaves that up to TCP's own dead-peer detection.
    # timeout_graceful_shutdown bounds how long Ctrl-C/SIGTERM waits for open
    # WebSocket and MJPEG connections to close before proceeding to the
    # lifespan shutdown (robot settle + park). uvicorn's default is unbounded,
    # so a browser left open on /pilot would otherwise keep the robot from
    # ever parking.
    uvicorn.run(
        app_instance,
        host="0.0.0.0",
        port=port,
        log_level="info",
        ws_ping_interval=5,
        ws_ping_timeout=5,
        timeout_graceful_shutdown=2,
    )


def run() -> None:
    """Console-script entry point (`[project.scripts]`): invoke the typer app."""
    app()


if __name__ == "__main__":  # pragma: no cover
    run()
