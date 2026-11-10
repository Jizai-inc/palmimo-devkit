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
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

import typer
import uvicorn

from palmimo_sdk import (
    Denoiser,
    DynamixelDriver,
    FaceDisplay,
    HeadCamera,
    MicStream,
    Palmimo,
    ServoDriver,
    find_face_port,
    resolve_alsa_device,
)

from .ai.audio import Playback
from .ai.controller import DEFAULT_MODEL, AiController, OpenMic
from .ai.reflexes import Detectors, Reflexes
from .control import MAX_GAIT_SPEED
from .modes import EditablePrompts, default_prompt_dir
from .robot_loop import RobotLoop
from .server import create_app
from .session import PilotSession
from .telemetry import Telemetry
from .video import VideoStream
from .voices import VoiceSetting, default_voice_path


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
#: per-frame speed scaling (unlike the gamepad example's stick deflection) --
#: buttons are on/off.
DEFAULT_GAIT_SPEED: float = 0.012

#: Speaking rate, as in the companion agent: 1.0 is the voice's natural pace,
#: lower is slower.
DEFAULT_VOICE_SPEED: float = 1.0

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


def _probe_face(port: str) -> FaceDisplay | None:
    """Open and close a `FaceDisplay` on *port*; return a fresh one if that worked.

    Mirrors `_probe_servo_driver`: the open that stays up for the run is
    `Palmimo.connect()`'s, from the server lifespan.
    """
    face = FaceDisplay(port=port)
    try:
        face.connect()
        face.disconnect()
    except Exception as exc:  # busy port, timeout, reservation held by another app
        _LOG.warning("face display on %s did not open (%s); running without a face", port, exc)
        return None
    return FaceDisplay(port=port)


def _build_face(
    *,
    no_face: bool,
    find_port: Callable[[], str | None] = find_face_port,
    probe: Callable[[str], FaceDisplay | None] = _probe_face,
) -> FaceDisplay | None:
    """Build a `FaceDisplay` only when one is plugged in and opens.

    `Palmimo.connect()` opens every attached resource, so a face that is
    missing or will not open would stop the whole server from starting.
    Without a face the controller runs as before, with no expressions.
    """
    if no_face:
        return None
    try:
        port = find_port()
    except Exception as exc:  # pyserial missing or the port scan failing
        _LOG.warning("face display detection failed (%s); running without a face", exc)
        return None
    if port is None:
        print("face display not found -- running without expressions")
        return None
    return probe(port)


def _build_robot(
    *, dry_run: bool, gait_speed: float, fps: int, servo_port: str | None, no_face: bool = False
) -> Palmimo:
    """Build the `Palmimo` facade: compute-only under `--dry-run`, else a probed `DynamixelDriver`
    and, when one is plugged in, the face display."""
    driver = None if dry_run else _build_servo_driver(servo_port)
    display = None if dry_run else _build_face(no_face=no_face)
    return Palmimo(gait_speed=gait_speed, fps=fps, driver=driver, display=display)


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
) -> OpenMic:
    """Open the microphone, preferring echo cancellation where *device* supports it.

    A device without echo cancellation (the booth's USB mic) opens raw at its
    own rate. `processors=[]` is passed explicitly there: `MicStream` reads a
    missing `processors` as "use my default echo canceller".

    No device name hint, matching the companion agent: the SDK picks whichever
    input exposes the six channels its default `EchoCanceller` needs, and the
    board's name varies between firmware builds. If no such input exists, fall
    back to noise removal only, so AI mode still works. The returned
    `echo_cancelled` says which happened: without it the listener must ignore
    the mic while the robot talks, or the robot hears itself.
    """
    if not device.echo_cancel:
        mic = open_stream(sample_rate=device.mic_rate, device_name_hint=device.mic_hint, processors=[])
        mic.open()
        return OpenMic(mic, echo_cancelled=False)
    try:
        mic = open_stream()
        mic.open()
        return OpenMic(mic, echo_cancelled=True)
    except RuntimeError as exc:
        _LOG.warning(
            "echo-cancelled capture unavailable (%s); using noise removal only, "
            "so the robot will not hear visitors while it talks",
            exc,
        )
    mic = open_stream(processors=[Denoiser()])
    mic.open()
    return OpenMic(mic, echo_cancelled=False)


@contextlib.contextmanager
def _open_mic(device: AudioDevice) -> Iterator[OpenMic]:
    """Open the microphone for one AI mode; closed again when the mode ends."""
    opened = _open_mic_stream(device)
    stream = cast(MicStream, opened.stream)
    try:
        yield opened
    finally:
        stream.close()


def _build_playback(device: AudioDevice) -> Playback:
    """Open playback on the speaker matching *device*, or ALSA's default if none matches."""
    playback = Playback(device=resolve_alsa_device(device.speaker_hint, kind="playback"))
    _LOG.info("AI speech plays on the device matching %r", device.speaker_hint)
    return playback


class _CachedDetectors:
    """Builds the wave and face detectors once per process and hands the same pair to every AI session.

    Loading imports MediaPipe and reads (on first run, downloads) its models,
    which takes seconds; a second AI session must not pay that again. A failed
    load is not cached, so a robot that was offline at its first AI session
    gets reflexes once the download works.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._detectors: Detectors | None = None

    def __call__(self) -> Detectors:
        with self._lock:
            if self._detectors is None:
                from .ai.vision import FaceLocator, WaveDetector

                self._detectors = Detectors(wave=WaveDetector(), face=FaceLocator())
            return self._detectors


def _build_reflexes_factory(
    session: PilotSession, video: VideoStream, telemetry: Telemetry, *, enabled: bool
) -> Callable[[], Reflexes] | None:
    """Build the per-session reflexes factory, or `None` when reflexes are off or there is no camera."""
    if not enabled:
        return None
    detectors = _CachedDetectors()

    def factory() -> Reflexes:
        return Reflexes(session, video.latest_frame, detectors, telemetry=telemetry)

    return factory


def _build_ai(
    session: PilotSession,
    telemetry: Telemetry,
    prompts: EditablePrompts,
    audio: AudioDevice,
    reflexes_factory: Callable[[], Reflexes] | None = None,
    voice_speed: float = DEFAULT_VOICE_SPEED,
    voices: VoiceSetting | None = None,
) -> AiController:
    """Build the AI controller. A missing `OPENAI_API_KEY` leaves AI unavailable, never teleop.

    `CONTROLLER_REALTIME_MODEL` swaps the Realtime model without a code
    change, e.g. to compare the full model against the mini default.
    """
    return AiController(
        session,
        prompts,
        api_key=os.environ.get("OPENAI_API_KEY") or None,
        telemetry=telemetry,
        mic_factory=functools.partial(_open_mic, audio),
        playback_factory=functools.partial(_build_playback, audio),
        model=os.environ.get("CONTROLLER_REALTIME_MODEL") or DEFAULT_MODEL,
        voice=(voices if voices is not None else VoiceSetting(default_voice_path())).get,
        voice_speed=voice_speed,
        reflexes_factory=reflexes_factory,
    )


app = typer.Typer(add_completion=False)


@app.command()
def main(
    port: int = typer.Option(DEFAULT_PORT, "--port", help="HTTP/WebSocket port to serve on."),
    fps: int = typer.Option(60, "--fps", help="Robot control loop rate in cycles per second.", min=1),
    gait_speed: float = typer.Option(
        DEFAULT_GAIT_SPEED, "--gait-speed", help="`Palmimo` gait speed.", min=0.001, max=MAX_GAIT_SPEED
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute-only: never attach a servo driver."),
    no_camera: bool = typer.Option(False, "--no-camera", help="Never attach the head camera."),
    no_face: bool = typer.Option(False, "--no-face", help="Never attach the face display."),
    no_reflexes: bool = typer.Option(
        False, "--no-reflexes", help="In AI modes, do not wave back at visitors or glance at faces."
    ),
    servo_port: str | None = typer.Option(
        None, "--servo-port", help="Servo bus serial port, e.g. /dev/ttyACM0 (auto-detected if omitted)."
    ),
    audio_device: AudioDeviceName = typer.Option(
        AudioDeviceName.RESPEAKER,
        "--audio-device",
        help="AI mode mic and speaker: respeaker (development robot) or booth-usb (exhibition robot).",
    ),
    voice_speed: float = typer.Option(
        DEFAULT_VOICE_SPEED,
        "--voice-speed",
        help="AI mode speaking rate: 1.0 is the voice's natural pace, lower is slower.",
        min=0.5,
        max=1.5,
    ),
) -> None:
    """Serve Palmimo Controller: a phone/laptop browser drives the robot, views its head
    camera, and switches to AI voice modes, on the same LAN, with no authentication."""
    logging.basicConfig(level=logging.INFO)
    robot = _build_robot(dry_run=dry_run, gait_speed=gait_speed, fps=fps, servo_port=servo_port, no_face=no_face)
    session = PilotSession()
    robot_loop = RobotLoop(robot, session, fps=fps)
    video = VideoStream(_build_camera(no_camera))
    telemetry = Telemetry()
    # One store for both: a prompt saved from the homepage must be the one the next AI mode reads.
    prompts = EditablePrompts(default_prompt_dir())
    # Likewise one store for the voice the homepage picks and the next AI mode speaks in.
    voices = VoiceSetting(default_voice_path())
    reflexes = _build_reflexes_factory(session, video, telemetry, enabled=not (no_reflexes or no_camera))
    ai = _build_ai(session, telemetry, prompts, AUDIO_DEVICES[audio_device], reflexes, voice_speed, voices)
    app_instance = create_app(robot, session, robot_loop, video, ai, prompts, telemetry, voices)

    mode = "--dry-run" if dry_run else ("compute-only (no servo bus found)" if robot.driver is None else "live")
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
