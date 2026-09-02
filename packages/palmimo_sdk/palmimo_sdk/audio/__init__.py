# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Audio processing layer: pure sample transforms, no capture / hardware I/O.

Mirrors :mod:`palmimo_sdk.io`'s ownership split, but on the other side of the
boundary: ``io`` owns hardware resources (mic, speaker, camera, servo bus) and
hands back raw bytes / arrays; ``audio`` only transforms those samples —
no ``sounddevice``, no shelling out, no device handles.

:class:`~palmimo_sdk.audio.denoise.SpeechDenoiser` and
:class:`~palmimo_sdk.audio.denoise.StreamingDenoiser` wrap sherpa-onnx's GTCRN
noise-removal model (offline / batch and streaming forms, respectively) —
carrying forward the earlier finding that denoising the whole mic stream
*before* speech-segment detection is what keeps servo drive noise from
breaking both segmentation and ASR. Both lazy-import ``sherpa_onnx`` /
``numpy`` at construction time, so importing this package stays
dependency-free without the ``voice`` extra.

:func:`~palmimo_sdk.audio.denoise.ensure_denoise_model` resolves the GTCRN
model's local path, auto-downloading it via
:func:`~palmimo_sdk.download.download_atomic` if missing, under the shared
cache root :func:`~palmimo_sdk.download.default_model_dir` (re-exported here,
where the SDK's first auto-downloaded model lived).

:class:`~palmimo_sdk.audio.processor.AudioProcessor` is the DI seam capture
resources push audio through — a WAV-bytes-in/WAV-bytes-out ``Protocol`` any
transform can implement and any number of which can be cascaded.
:class:`~palmimo_sdk.audio.processor.Denoiser` is the bundled streaming
implementation, wrapping :class:`StreamingDenoiser` behind that contract;
:class:`~palmimo_sdk.audio.processor.ClipDenoiser` is the offline counterpart
for a complete clip, wrapping :class:`SpeechDenoiser` instead.
:func:`~palmimo_sdk.audio.processor.int16_to_wav` /
:func:`~palmimo_sdk.audio.processor.wav_to_int16` /
:func:`~palmimo_sdk.audio.processor.wav_to_int16_multi` are the shared WAV <->
int16 ndarray helpers it (and the capture layer) use — the last for a
multi-channel WAV, since :func:`wav_to_int16` itself requires mono.

:class:`~palmimo_sdk.audio.aec.EchoCanceller` is the bundled DTLN-AEC
implementation of the same :class:`AudioProcessor` contract — it cancels a
loudspeaker's echo out of a microphone channel given a reference channel from
the same read, and is :class:`~palmimo_sdk.io.mic_stream.MicStream`'s default
processor. Unlike ``Denoiser``, it consumes a multi-channel WAV (mic +
loudspeaker reference) and always produces mono; see its ``capture_channels``.
:func:`~palmimo_sdk.audio.dtln.ensure_dtln_models` resolves the DTLN-AEC
models' local paths, auto-downloading them the same way
:func:`ensure_denoise_model` does for GTCRN.
"""

from .aec import EchoCanceller
from .denoise import (
    GTCRN_MODEL_SHA256,
    GTCRN_MODEL_URL,
    SpeechDenoiser,
    StreamingDenoiser,
    default_model_dir,
    ensure_denoise_model,
)
from .dtln import ensure_dtln_models
from .processor import (
    AudioProcessor,
    ClipDenoiser,
    Denoiser,
    int16_to_wav,
    wav_to_int16,
    wav_to_int16_multi,
)


__all__ = [
    "GTCRN_MODEL_SHA256",
    "GTCRN_MODEL_URL",
    "AudioProcessor",
    "ClipDenoiser",
    "Denoiser",
    "EchoCanceller",
    "SpeechDenoiser",
    "StreamingDenoiser",
    "default_model_dir",
    "ensure_denoise_model",
    "ensure_dtln_models",
    "int16_to_wav",
    "wav_to_int16",
    "wav_to_int16_multi",
]
