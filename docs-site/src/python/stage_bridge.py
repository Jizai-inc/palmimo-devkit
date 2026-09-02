"""What passes between the browser console and the stage above it.

Two directions. Out: the stage turns each node about its own +Z, and the model
was exported so that turn counts the way the SDK counts, but the SDK hands
back ticks rather than angles -- so undoing its servo encoding (neutral, and
the left side's mirrored pitch) happens here, from the SDK's own constants.
Joints the SDK does not drive are absent from the result, and the stage leaves
a joint it is not handed exactly where it stands.

In: three of the four peripherals have a stand-in backed by the stage --
:class:`StageCamera`, :class:`StageDisplay` and :class:`StageMic`. Nothing in
the SDK was told about any of them: ``Palmimo`` calls a handful of methods on
whatever it was handed, and these have those methods. What the SDK does not do
is publish the shape, so :class:`CameraSource`, :class:`MicSource` and
:class:`DisplaySink` below write it down on this side of the bridge, where a
reader can run ``isinstance`` against it and a test can hold the bundled
classes to it.

The speaker is the one that cannot be stood in for: what
:class:`~palmimo_sdk.io.speaker.Speaker`'s ``say`` returns is a handle that
reaches back into the speaker that made it, so a stand-in would have to build
one. A page has no piper anyway, so nothing is lost here that could have
worked -- but it is why this file has no ``StageSpeaker`` beside the other
three.

Each is honest about the one way it differs from the hardware, in its own
docstring: a browser has no servo bus, and no business asking a reader for
their microphone.
"""

from __future__ import annotations

import io
import math
import time
import wave
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from palmimo_sdk import kinematics


LEGS: tuple[int, ...] = kinematics.LEFT_LEGS + kinematics.RIGHT_LEGS
# The neck's servos carry no mirroring and no neutral trim of their own, so a
# tick above neutral is the joint's own positive direction.
NECK_JOINTS: tuple[str, ...] = ("neck_yaw", "neck_pitch1")


def joint_angles(ticks: Mapping[str, int]) -> dict[str, float]:
    """Servo ticks as the stage's joint angles.

    Args:
        ticks: One control cycle's servo positions, as ``Palmimo.step()``
            returns them.

    Returns:
        dict[str, float]: Joint node name -> angle in radians.
    """
    angles: dict[str, float] = {}
    for leg in LEGS:
        if f"leg_{leg}_yaw" not in ticks:
            continue
        # Yaw counts down from neutral and the left side's pitch is mirrored:
        # the encoding leg_servo_ticks applies, read back the other way.
        pitch_sign = 1 if leg in kinematics.LEFT_LEGS else -1
        yaw_deg = (kinematics.NEUTRAL - ticks[f"leg_{leg}_yaw"]) / kinematics.TICK_PER_DEG
        angles[f"leg_{leg}_yaw"] = math.radians(yaw_deg)
        for pitch in ("pitch1", "pitch2"):
            name = f"leg_{leg}_{pitch}"
            deg = (ticks[name] - kinematics.NEUTRAL) / (kinematics.TICK_PER_DEG * pitch_sign)
            angles[name] = math.radians(deg)
    for joint in NECK_JOINTS:
        if joint in ticks:
            offset = (ticks[joint] - kinematics.NEUTRAL) / kinematics.TICK_PER_DEG
            angles[joint] = math.radians(offset)
    return angles


class StageFrame(bytes):
    """One frame off the stage: BGR bytes, top row first, with its shape.

    A real ``HeadCamera`` hands back a numpy array. There is no numpy in this
    page -- the interpreter ships without it, and fetching it would be the one
    thing this site loads from somewhere that is not itself -- so the frame is
    the bytes themselves, carrying the ``shape`` that says how to read them.
    ``numpy.frombuffer(frame, "uint8").reshape(frame.shape)`` is the array, for
    a reader who has numpy to hand.
    """

    shape: tuple[int, int, int]

    def __new__(cls, data: bytes, height: int, width: int) -> StageFrame:
        frame = super().__new__(cls, data)
        frame.shape = (height, width, 3)
        return frame

    def __repr__(self) -> str:
        # bytes' own repr would spill every pixel across the console.
        height, width, _ = self.shape
        return f"<StageFrame {width}x{height} BGR, {len(self)} bytes>"


try:  # pragma: no cover - resolves only inside the interpreter the page runs
    from pyodide.ffi import JsException as _StageError
except ImportError:

    class _StageError(Exception):  # type: ignore[no-redef]
        """Nothing raises this off the page, where there is no stage to call."""


def _stage() -> Any | None:
    """The stage, once the page has one -- ``None`` before, and off the page.

    Read on each use rather than held: the console starts before the stage
    finishes loading its model, so the first camera a reader opens is opened
    against a page that has nothing to show yet.
    """
    try:
        import js
    except ImportError:  # pragma: no cover - only outside the browser
        return None
    return getattr(js, "palmimoStage", None)


@runtime_checkable
class CameraSource(Protocol):
    """What ``Palmimo`` needs of a camera, written down.

    The SDK types ``Palmimo(camera=...)`` as the concrete ``HeadCamera`` and
    publishes no contract to name, so the shape it actually uses is recorded
    here instead. Python does not check an annotation at run time -- a
    stand-in of this shape is accepted either way -- so what these add is a way
    to *say* which shape, and to be told when saying it stops being true.

    The members are exactly what the facade calls plus what a reader reaching
    through ``robot.camera`` calls: the facade drives ``open``/``start_drain``/
    ``close``, and the frames come back through ``read`` and ``latest``.
    ``robot-runtime.test.mjs`` holds the bundled ``HeadCamera`` against this,
    so a facade that grows a new call is caught here rather than on the robot.
    """

    @property
    def is_open(self) -> bool: ...

    def open(self) -> None: ...

    def close(self) -> None: ...

    def start_drain(self) -> None: ...

    def read(self) -> tuple[bool, Any]: ...

    def latest(self, *, timeout: float = 2.0) -> Any | None: ...

    def add_consumer(self, fn: Callable[[Any, float], None]) -> None: ...


@runtime_checkable
class MicSource(Protocol):
    """What ``Palmimo`` needs of a microphone. See :class:`CameraSource`.

    Satisfied by both ``Microphone`` (one-shot) and ``MicStream`` (shared
    streaming capture) -- the facade never branches on which one it holds.
    ``record`` is here because the facade grows no recording method of its
    own; a consumer reaching through ``robot.mic`` is where audio is taken.
    """

    @property
    def is_open(self) -> bool: ...

    def open(self) -> None: ...

    def close(self) -> None: ...

    def record(self, seconds: float) -> bytes | None: ...


@runtime_checkable
class DisplaySink(Protocol):
    """What ``Palmimo`` needs of a face display. See :class:`CameraSource`.

    Keeps ``connect``/``disconnect`` rather than the others'
    ``open``/``close``, mirroring ``FaceDisplay`` -- this describes the real
    class rather than redesigning it.
    """

    @property
    def is_connected(self) -> bool: ...

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def wake(self) -> str: ...

    def idle(self) -> str: ...

    def set_expression(self, name: str, hold_ms: int = 0) -> str: ...


class StageCamera:
    """The robot's head camera, over the stage this page is drawing.

    A :class:`CameraSource`, so ``Palmimo`` owns it exactly as it owns a
    ``HeadCamera`` on the real robot: :meth:`open` and :meth:`close` are what
    ``connect()`` and ``disconnect()`` drive, and a reader reaches the frames
    through ``robot.camera``.
    """

    def __init__(self) -> None:
        self._open = False
        self._consumers: list[Callable[[Any, float], None]] = []

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        """Idempotent, like the camera this stands in for."""
        self._open = True

    def close(self) -> None:
        self._open = False

    def start_drain(self) -> None:
        """Nothing to start: the stage draws every frame whether or not anyone
        is reading, so there is no background thread here to keep one warm."""

    def read(self) -> tuple[bool, StageFrame | None]:
        """Grab one frame, as ``(ok, frame)``.

        A closed camera, a page whose stage has not started, and a readback the
        browser refuses all report the way the real one reports a device it
        could not open: ``ok`` is False and there is no frame, rather than an
        exception. Losing the WebGL context -- a suspended tab, a reset GPU --
        is this camera's unplugged cable, and a reader at the prompt should get
        the answer they would get from a real one.
        """
        stage = _stage()
        if not self._open or stage is None:
            return False, None
        try:
            picture = stage.read()
        except _StageError:
            return False, None
        frame = StageFrame(bytes(picture.data.to_py()), picture.height, picture.width)
        now = time.time()
        for consume in self._consumers:
            consume(frame, now)
        return True, frame

    def latest(self, *, timeout: float = 2.0) -> StageFrame | None:
        """The newest frame. Every frame is the newest one here -- the stage is
        drawing live, so there is no drained backlog for *timeout* to wait on."""
        ok, frame = self.read()
        return frame if ok else None

    def add_consumer(self, fn: Callable[[Any, float], None]) -> None:
        """Call *fn* with each frame :meth:`read` grabs, as the real drain does."""
        self._consumers.append(fn)


class StageDisplay:
    """The robot's face display, over the screen the stage draws on its head.

    A :class:`DisplaySink`. The replies are shaped like the firmware's own
    (``OK HAPPY``) because that is what the contract returns and what a reader
    comparing this page against the hardware would expect to read back.

    The faces are the stage's reading of the twelve the firmware ships, not a
    copy of its artwork -- see ``robot-face.js``.
    """

    def __init__(self) -> None:
        self._connected = False
        self.expression = "IDLE"

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def wake(self) -> str:
        """Host-boot-complete, the way the real display marks it: IDEA, then IDLE."""
        return self.set_expression("IDEA")

    def idle(self) -> str:
        return self.set_expression("IDLE")

    def set_expression(self, name: str, hold_ms: int = 0) -> str:
        """Show *name*. ``hold_ms`` is accepted and ignored: holding for a time
        and then returning to IDLE is the firmware's own timer, and this page
        has no thread to run it on."""
        wanted = name.strip().upper()
        stage = _stage()
        if stage is None:
            return "ERR NO STAGE"
        try:
            stage.face(wanted)
        except Exception:
            return f"ERR {wanted}"
        self.expression = wanted
        return f"OK {wanted}"


class StageMic:
    """The robot's microphone -- the shape of one, without listening to you.

    A :class:`MicSource`. **This page never asks for your microphone.** A landing
    page that opens a permission prompt to demonstrate an API has taken
    something it was not offered, so :meth:`record` returns a real, correctly
    formed WAV of the length asked for, containing silence. The header, the
    duration and the byte count are all genuine; the audio is not.
    """

    RATE = 16000
    CHANNELS = 1
    SAMPLE_WIDTH = 2

    def __init__(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def record(self, seconds: float) -> bytes | None:
        """A WAV of *seconds*, the way ``Microphone.record`` returns one.

        The real one opens a closed device rather than refusing, and reports
        every failure as ``None`` without raising. Both are copied here: a
        reader at the console should not meet a traceback the robot would not
        have given them.
        """
        self.open()
        if seconds <= 0:
            return None
        stage = _stage()
        if stage is not None:
            stage.listen(seconds)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(self.CHANNELS)
            wav.setsampwidth(self.SAMPLE_WIDTH)
            wav.setframerate(self.RATE)
            wav.writeframes(bytes(round(seconds * self.RATE) * self.CHANNELS * self.SAMPLE_WIDTH))
        return buffer.getvalue()
