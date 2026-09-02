# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""TTS engine backends behind the :class:`~palmimo_sdk.io.tts.base.TtsEngine`
boundary. :class:`~palmimo_sdk.io.speaker.Speaker` consumes this boundary and
never imports a concrete engine module directly (:class:`PiperEngine` is its
default, constructed for callers that don't inject their own engine).

:class:`OpenAiEngine` synthesizes off the robot, trading a network dependency
for speed and clarity a Pi cannot reach locally."""

from .base import TtsEngine, TtsVoice
from .openai import OpenAiEngine
from .piper import PiperEngine


__all__ = [
    "OpenAiEngine",
    "PiperEngine",
    "TtsEngine",
    "TtsVoice",
]
