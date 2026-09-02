# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""palmimo_sdk — the single window into the Palmimo software stack.

Users import the facade :class:`Palmimo` and (when driving hardware) attach a
:class:`ServoDriver`; everything else (gait/IK computation, servo I/O) lives
behind them.

    from palmimo_sdk import Palmimo

    robot = Palmimo()              # compute-only (sim / tests)
    robot.forward()
    for _ in range(100):
        pos = robot.step()      # dict like {"leg_1_yaw": 2048, ...}

To drive hardware, attach a concrete :class:`ServoDriver` and use the ``with``
protocol (connect / neutral-return / disconnect). :class:`DynamixelDriver` is
the bundled hardware backend (Dynamixel serial bus); it needs the optional
``palmimo-sdk[hardware]`` extra. With no driver attached the facade is
compute-only, and any :class:`ServoDriver` (e.g. the in-memory fake used in the
tests) can be injected by the caller.

Peripherals hang off the same facade: :class:`FaceDisplay`, :class:`Speaker`,
:class:`HeadCamera`, and :class:`Microphone` (or :class:`MicStream`, for a
shared streaming mic) are attached as
``Palmimo(display=..., speaker=..., camera=..., mic=...)`` and share the driver's
connect/disconnect lifecycle. Their ``*Config`` companions are exported
alongside them.

:class:`MicStream` and :class:`Microphone` push captured audio through a
``processors`` chain of :class:`AudioProcessor` (a WAV-bytes-in/WAV-bytes-out
``Protocol``) before handing it to consumers; :class:`EchoCanceller` (DTLN-AEC)
is :class:`MicStream`'s default, cancelling a loudspeaker's echo out of a
microphone channel given a reference channel from the same read. :class:`Denoiser`
is the bundled GTCRN-based noise-removal implementation for a continuous
stream — the fallback when the capture device lacks the extra channel
``EchoCanceller`` needs — and :class:`ClipDenoiser` is its offline counterpart
for a complete, self-contained clip (e.g. :class:`Microphone`'s recordings).

:class:`Speaker` delegates synthesis to a pluggable :class:`TtsEngine`
(:class:`PiperEngine`, over ``piper-plus``, is its default); pass a different
:class:`TtsEngine` to ``Speaker(engine=...)`` to swap backends —
:class:`OpenAiEngine` is the other one built in.
:class:`TtsVoice` is the per-utterance handle an engine's ``load_voice``
returns.

:func:`find_servo_port` auto-detects the servo bus's serial port (its
USB-to-servo bridge) without hard-coding platform-specific paths;
:class:`PortDetectionError` is raised when detection fails.
:class:`DynamixelDriver` runs the same detection itself when built with
``port=None`` (the default).
"""

from . import kinematics
from .audio import AudioProcessor, ClipDenoiser, Denoiser, EchoCanceller
from .engine import Motion, MotionEngine
from .io import (
    SUPPORTED_MOTOR_MODELS,
    DynamixelConnectTimeoutError,
    DynamixelDriver,
    FaceDisplay,
    FaceDisplayConnectTimeoutError,
    FaceDisplayError,
    HeadCamera,
    HeadCameraConfig,
    Microphone,
    MicrophoneConfig,
    MicStream,
    OpenAiEngine,
    PiperEngine,
    PortDetectionError,
    ServoDriver,
    ServoTelemetry,
    Speaker,
    SpeakerConfig,
    SpeechHandle,
    Subscription,
    TtsEngine,
    TtsVoice,
    find_face_port,
    find_servo_port,
    palmimo_motor_ids,
    resolve_alsa_device,
)
from .name_match import PALMIMO_NAMES, NameMatch, NameMatcher, name_skeleton
from .robot import (
    MotionCancelled,
    NeckPitchDegrees,
    NeckPitchNormalized,
    NeckYawDegrees,
    NeckYawNormalized,
    Palmimo,
    RoutineStep,
)


__all__ = [
    "PALMIMO_NAMES",
    "SUPPORTED_MOTOR_MODELS",
    "AudioProcessor",
    "ClipDenoiser",
    "Denoiser",
    "DynamixelConnectTimeoutError",
    "DynamixelDriver",
    "EchoCanceller",
    "FaceDisplay",
    "FaceDisplayConnectTimeoutError",
    "FaceDisplayError",
    "HeadCamera",
    "HeadCameraConfig",
    "MicStream",
    "Microphone",
    "MicrophoneConfig",
    "Motion",
    "MotionCancelled",
    "MotionEngine",
    "NameMatch",
    "NameMatcher",
    "NeckPitchDegrees",
    "NeckPitchNormalized",
    "NeckYawDegrees",
    "NeckYawNormalized",
    "OpenAiEngine",
    "Palmimo",
    "PiperEngine",
    "PortDetectionError",
    "RoutineStep",
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
    "kinematics",
    "name_skeleton",
    "palmimo_motor_ids",
    "resolve_alsa_device",
]
