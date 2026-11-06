"""`palmimo-controller` CLI: build the robot, session, video, and AI wiring, then serve the web app.

All the real wiring -- which driver, which camera -- lives in this module's
`_build_robot`/`_build_camera` helpers, kept close to the CLI options that
choose them; `server.create_app` only ever sees the already-built pieces.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum

import typer
import uvicorn

from palmimo_sdk import (
    Denoiser,
    DynamixelDriver,
    HeadCamera,
    MicStream,
    Palmimo,
    ServoDriver,
    resolve_alsa_device,
)

from .ai.audio import Playback
from .ai.controller import AiController
from .modes import EditablePrompts, default_prompt_dir
from .robot_loop import RobotLoop
from .server import create_app
from .session import PilotSession
from .telemetry import Telemetry
from .video import VideoStream


# Released SDK versions before reservations have no reservation exceptions.
_RESERVATION_ERRORS: tuple[type[Exception], ...]
try:
    from palmimo_sdk.reservation import ReservationError
except ImportError:
    _RESERVATION_ERRORS = ()
else:
    _RESERVATION_ERRORS = (ReservationError,)


_LOG = logging.getLogger(__name__)

#: Default gait speed applied to every teleop-driven motion. There is no
#: per-frame speed scaling here (unlike the gamepad example's stick
#: deflection) -- buttons are on/off, so one fixed speed is the whole knob.
DEFAULT_GAIT_SPEED: float = 0.012

#: Avoids 8765 (the MCP server), 80/8080 (Palmimo Portal), and 8000 (the
#: teleop app), so both apps can be installed on one robot.
DEFAULT_PORT: int = 8010


class AudioDeviceName(StrEnum):
    """Which mic and speaker the robot has; the `--audio-device` choices."""

    RESPEAKER = "respeaker"
    BOOTH_USB = "booth-usb"


@dataclass(frozen=True)
class AudioDevice:
    """How to open one robot's mic and speaker for an AI session."""

    #: Capture device, matched by name; `None` lets the SDK pick the input its echo canceller needs.
    mic_hint: str | None
    mic_rate: int
    #: Playback device, matched by name in `aplay -l`.
    speaker_hint: str
    echo_cancel: bool


#: Matches the companion agent's `audio_device` choices. A development robot
#: has a ReSpeaker array (mic and speaker on one board, so echo cancellation
#: works). An exhibition robot has a push-to-talk USB mic, which opens only at
#: 48 kHz and has no loopback channel to cancel echo against, and a separate
#: USB speaker that `aplay -l` lists as "Composite".
AUDIO_DEVICES: dict[AudioDeviceName, AudioDevice] = {
    AudioDeviceName.RESPEAKER: AudioDevice(mic_hint=None, mic_rate=16000, speaker_hint="ReSpeaker", echo_cancel=True),
    AudioDeviceName.BOOTH_USB: AudioDevice(
        mic_hint="USB PnP", mic_rate=48000, speaker_hint="Composite", echo_cancel=False
    ),
}

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
    except _RESERVATION_ERRORS:
        raise
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


def _open_mic_stream(
    device: AudioDevice = AUDIO_DEVICES[AudioDeviceName.RESPEAKER],
    *,
    open_stream: Callable[..., MicStream] = MicStream,
) -> MicStream:
    """Open the microphone, preferring echo cancellation where *device* supports it.

    A device without echo cancellation (the booth's USB mic) opens raw at its
    own rate. `processors=[]` is passed explicitly there: `MicStream` reads a
    missing `processors` as "use my default echo canceller".

    No device name hint, matching the companion agent: the SDK picks whichever
    input exposes the six channels its default `EchoCanceller` needs, and the
    board's name varies between firmware builds. If no such input exists, fall
    back to noise removal only, so AI mode still works -- but without echo
    cancellation the robot can hear its own voice and cut itself off.
    """
    if not device.echo_cancel:
        mic = open_stream(sample_rate=device.mic_rate, device_name_hint=device.mic_hint, processors=[])
        mic.open()
        return mic
    try:
        mic = open_stream()
        mic.open()
        return mic
    except RuntimeError as exc:
        _LOG.warning(
            "echo-cancelled capture unavailable (%s); using noise removal only, "
            "so the robot may hear and interrupt its own voice",
            exc,
        )
    mic = open_stream(processors=[Denoiser()])
    mic.open()
    return mic


@contextlib.contextmanager
def _open_mic(device: AudioDevice) -> Iterator[MicStream]:
    """Open the microphone for one AI session; closed again when the session ends."""
    mic = _open_mic_stream(device)
    try:
        yield mic
    finally:
        mic.close()


def _build_playback(device: AudioDevice) -> Playback:
    alsa_device = resolve_alsa_device(device.speaker_hint, kind="playback")
    _LOG.info("AI speech plays on ALSA device %s", alsa_device or "default")
    return Playback(device=alsa_device)


def _build_ai(
    session: PilotSession, telemetry: Telemetry, prompts: EditablePrompts, audio: AudioDevice
) -> AiController:
    """Build the AI controller. A missing `OPENAI_API_KEY` leaves AI unavailable, never teleop."""
    return AiController(
        session,
        prompts,
        api_key=os.environ.get("OPENAI_API_KEY"),
        telemetry=telemetry,
        mic_factory=functools.partial(_open_mic, audio),
        playback_factory=functools.partial(_build_playback, audio),
    )


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
    audio_device: AudioDeviceName = typer.Option(
        AudioDeviceName.RESPEAKER,
        "--audio-device",
        help="AI mode mic and speaker: respeaker (development robot) or booth-usb (exhibition robot).",
    ),
) -> None:
    """Serve Palmimo Controller: a phone/laptop browser drives the robot, views its head
    camera, and switches to AI voice modes, on the same LAN, with no authentication."""
    logging.basicConfig(level=logging.INFO)
    robot = _build_robot(dry_run=dry_run, gait_speed=gait_speed, fps=fps, servo_port=servo_port)
    session = PilotSession()
    robot_loop = RobotLoop(robot, session, fps=fps)
    video = VideoStream(_build_camera(no_camera))
    telemetry = Telemetry()
    # One store for both: a prompt saved from the homepage must be the one the next AI mode reads.
    prompts = EditablePrompts(default_prompt_dir())
    ai = _build_ai(session, telemetry, prompts, AUDIO_DEVICES[audio_device])
    app_instance = create_app(robot, session, robot_loop, video, ai, prompts, telemetry)

    mode = (
        "--dry-run"
        if dry_run
        else ("compute-only (no servo bus found)" if not robot.has_connectable_resource else "live")
    )
    ai_state = "AI modes available" if ai.available else "AI modes off: OPENAI_API_KEY not set"
    print(
        f"Palmimo Controller starting on http://0.0.0.0:{port} ({mode}; {ai_state}). Open /pilot to drive, / to view.",
        flush=True,
    )
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
