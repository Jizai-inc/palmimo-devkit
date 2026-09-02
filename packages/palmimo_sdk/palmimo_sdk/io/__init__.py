# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""I/O layer: the SDK owns each hardware resource; upper layers consume them.

:class:`~palmimo_sdk.io.base.ServoDriver` is the servo contract; :class:`DynamixelDriver`
is the hardware backend over a Dynamixel serial bus. The driver only pulls in
``dynamixel_sdk`` lazily (optional ``palmimo-sdk[hardware]`` extra), so importing
this package stays hardware-free for compute-only use.

:func:`~palmimo_sdk.io.dynamixel.find_servo_port` auto-detects the servo bus's
serial port (it lives with :class:`DynamixelDriver`, whose bus it locates);
:class:`~palmimo_sdk.io.dynamixel.PortDetectionError` is raised when detection
fails (zero candidates or multiple ambiguous candidates).

:class:`~palmimo_sdk.io.camera.HeadCamera` is the head camera resource — the single
owner of the head camera's UVC capture (opening ``cv2.VideoCapture``). Its optional
background drain (``start_drain`` / ``add_consumer`` / ``latest``) is what lets
several readers share one physical camera instead of each opening their own
device.
Like the driver, it imports OpenCV lazily so this package stays hardware-free
to import.

:class:`~palmimo_sdk.io.microphone.Microphone` is the USB mic resource — the single
owner of the ``arecord`` / ``rec`` capture, returning raw WAV bytes that the
voice listener feeds to speech recognition. It shells out lazily, so this
package stays hardware-free to import.

:class:`~palmimo_sdk.io.mic_stream.MicStream` is the shared streaming mic
resource — owns one ``sounddevice.InputStream`` on a background thread and
fans small chunks out to multiple subscribers (an always-on VAD alongside an
on-demand ASR capture, say), so they don't fight over the one physical device.
It imports ``sounddevice`` / ``numpy`` lazily (the ``palmimo-sdk[voice]``
extra), so this package stays dependency-free to import. A same-``device_key``
:class:`Microphone` cooperates with it automatically — see
:mod:`palmimo_sdk.io._mic_registry`.

:class:`~palmimo_sdk.io.speaker.Speaker` is the text-to-speech resource — the single
owner of speech queueing/playback, so higher layers just hand it text. Synthesis
itself is delegated to a pluggable :class:`~palmimo_sdk.io.tts.base.TtsEngine`
(default :class:`~palmimo_sdk.io.tts.piper.PiperEngine`, over ``piper-plus``),
which shells out / imports its backend lazily, so this package stays
hardware-free to import.

:class:`~palmimo_sdk.io.display.FaceDisplay` is the host-side client for the
face display over USB-CDC; like the driver it imports ``pyserial`` lazily, so
this package stays hardware-free to import.
"""

from .alsa_devices import resolve_alsa_device
from .base import ServoDriver, ServoTelemetry
from .camera import HeadCamera, HeadCameraConfig
from .display import FaceDisplay, FaceDisplayConnectTimeoutError, FaceDisplayError, find_face_port
from .dynamixel import (
    SUPPORTED_MOTOR_MODELS,
    DynamixelConnectTimeoutError,
    DynamixelDriver,
    PortDetectionError,
    find_servo_port,
    palmimo_motor_ids,
)
from .mic_stream import MicStream, Subscription
from .microphone import Microphone, MicrophoneConfig
from .speaker import Speaker, SpeakerConfig, SpeechHandle
from .tts import OpenAiEngine, PiperEngine, TtsEngine, TtsVoice


__all__ = [
    "SUPPORTED_MOTOR_MODELS",
    "DynamixelConnectTimeoutError",
    "DynamixelDriver",
    "FaceDisplay",
    "FaceDisplayConnectTimeoutError",
    "FaceDisplayError",
    "HeadCamera",
    "HeadCameraConfig",
    "MicStream",
    "Microphone",
    "MicrophoneConfig",
    "OpenAiEngine",
    "PiperEngine",
    "PortDetectionError",
    "ServoDriver",
    "ServoTelemetry",
    "Speaker",
    "SpeakerConfig",
    "SpeechHandle",
    "Subscription",
    "TtsEngine",
    "TtsVoice",
    "find_face_port",
    "find_servo_port",
    "palmimo_motor_ids",
    "resolve_alsa_device",
]
