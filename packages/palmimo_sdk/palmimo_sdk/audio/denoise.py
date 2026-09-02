# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""GTCRN (sherpa-onnx) noise removal — the pure processing layer for voice capture.

Exhibition operation surfaced a recurring failure mode: servo drive noise leaks
into the mic, and on raw audio both speech-segment detection (volume peaks) and
ASR break. The fix that shipped was denoise-first — run the whole mic stream
through a streaming denoiser *before* segmenting, so causal-model warm-up
issues (denoising each utterance in isolation attenuates it) never appear
structurally. Backend comparison on drive-noise recordings settled on GTCRN
over DPDFNet: near-perfect ASR accuracy at RTF≈0.045 @1 thread vs DPDFNet's
lower accuracy and RTF≈0.106. This module carries that decision
forward as the SDK's ``audio`` layer, dropping backend selection (GTCRN only)
and the ctypes Online C API in favor of sherpa-onnx's public Python
``OnlineSpeechDenoiser`` binding (>=1.13.0).

Like :mod:`palmimo_sdk.io`, this module is a pure layer: no sounddevice, no
hardware I/O. It only transforms sample arrays / WAV bytes; capturing them is
the job of ``io.microphone`` / ``io.MicStream``, one layer up. ``sherpa_onnx``
and ``numpy`` are lazy-imported inside the classes that need them (voice
extra), so ``import palmimo_sdk`` stays dependency-free.
"""

from __future__ import annotations

import logging
import os
import time
import wave
from collections.abc import Iterable, Iterator
from io import BytesIO
from pathlib import Path
from typing import Any

from palmimo_sdk.download import default_model_dir, download_atomic


GTCRN_MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speech-enhancement-models/gtcrn_simple.onnx"
GTCRN_MODEL_SHA256 = "e77603ac0c23dac3227dd2d7135b3a585cbee2679048aecfa886657d3ae1b534"

_MODEL_FILENAME = "gtcrn_simple.onnx"


def ensure_denoise_model(path: str | None = None) -> str:
    """Resolve the GTCRN model path, auto-downloading it if missing.

    Args:
        path: Explicit model path. ``~`` is expanded. When ``None``, defaults
            to ``default_model_dir() / "gtcrn_simple.onnx"``.

    Returns:
        The resolved, existing model path.

    Raises:
        RuntimeError: The model was missing and the download failed (e.g. no
            network). The message points to placing the file manually and
            passing ``path`` for offline use.
    """
    resolved = Path(os.path.expanduser(path)) if path is not None else default_model_dir() / _MODEL_FILENAME
    if not resolved.exists():
        try:
            download_atomic(GTCRN_MODEL_URL, resolved, timeout=60.0, sha256=GTCRN_MODEL_SHA256)
        except OSError as exc:
            raise RuntimeError(
                f"failed to download GTCRN denoise model from {GTCRN_MODEL_URL} to {resolved}: {exc}. "
                "In an offline environment, download the file manually and pass its path explicitly "
                "(e.g. SpeechDenoiser(model_path=...) / ensure_denoise_model(path=...))."
            ) from exc
    return str(resolved)


class SpeechDenoiser:
    """GTCRN (sherpa-onnx ``OfflineSpeechDenoiser``) noise removal — offline (batch) form.

    The primary pipeline should use :class:`StreamingDenoiser`; this class is
    for one-shot use over an already-captured clip (e.g. denoising a WAV file
    on disk or the bytes returned by ``Microphone.record()``).

    ``sherpa_onnx`` / ``numpy`` are lazy-imported at construction time so that
    importing ``palmimo_sdk`` stays dependency-free without the ``voice`` extra.
    """

    def __init__(self, model_path: str, *, sample_rate: int = 16000, num_threads: int = 1) -> None:
        import numpy as np
        import sherpa_onnx

        self._np = np
        config = sherpa_onnx.OfflineSpeechDenoiserConfig(
            model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(
                gtcrn=sherpa_onnx.OfflineSpeechDenoiserGtcrnModelConfig(model=model_path),
                num_threads=num_threads,
                debug=False,
                provider="cpu",
            )
        )
        self._denoiser = sherpa_onnx.OfflineSpeechDenoiser(config)
        self._sample_rate = sample_rate
        self._log = logging.getLogger(__name__)

    def denoise(self, samples_int16: Any) -> Any:
        """Denoise a mono int16 1-D sample array, returning a mono int16 1-D array.

        An empty input is returned unchanged (some backends error on an empty
        array, so ``run`` is never called for one). If the model returns a
        ``sample_rate`` different from what this instance was configured with,
        a warning is logged and the original (un-denoised) samples are
        returned instead, so callers can rely on the output always matching
        the configured sample rate.
        """
        if len(samples_int16) == 0:
            return samples_int16
        np = self._np
        t0 = time.perf_counter()
        x = samples_int16.astype(np.float32) / 32768.0
        result = self._denoiser.run(x, self._sample_rate)
        infer_ms = (time.perf_counter() - t0) * 1000.0
        self._log.debug("denoise: %.1f ms (%d samples)", infer_ms, len(samples_int16))
        if result.sample_rate != self._sample_rate:
            self._log.warning(
                "denoiser returned unexpected sample_rate=%d (expected %d); "
                "falling back to the original (un-denoised) audio",
                result.sample_rate,
                self._sample_rate,
            )
            return samples_int16
        out = np.asarray(result.samples, dtype=np.float32)
        return np.clip(out * 32768.0, -32768, 32767).astype(np.int16)

    def denoise_wav(self, wav_bytes: bytes) -> bytes:
        """Denoise a mono/16-bit WAV byte stream, returning WAV bytes.

        This is what lets ``Microphone.record()``'s WAV output feed straight
        into denoising (``denoiser.denoise_wav(mic.record(...))``).

        Raises:
            ValueError: ``wav_bytes`` is not mono, not 16-bit, or its frame
                rate does not match this instance's configured ``sample_rate``.
        """
        np = self._np
        with wave.open(BytesIO(wav_bytes), "rb") as reader:
            n_channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            frame_rate = reader.getframerate()
            n_frames = reader.getnframes()
            raw = reader.readframes(n_frames)
        if n_channels != 1:
            raise ValueError(f"expected mono WAV, got {n_channels} channels")
        if sample_width != 2:
            raise ValueError(f"expected 16-bit WAV, got {sample_width * 8}-bit")
        if frame_rate != self._sample_rate:
            raise ValueError(f"expected {self._sample_rate} Hz WAV, got {frame_rate} Hz")

        samples = np.frombuffer(raw, dtype=np.int16)
        denoised = self.denoise(samples)

        buf = BytesIO()
        with wave.open(buf, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(self._sample_rate)
            writer.writeframes(np.asarray(denoised, dtype=np.int16).tobytes())
        return buf.getvalue()


class StreamingDenoiser:
    """GTCRN (sherpa-onnx ``OnlineSpeechDenoiser``) noise removal — streaming form.

    Meant for a continuous mic stream: feed successive chunks to
    :meth:`process` (e.g. from ``MicStream.stream()``). Uses sherpa-onnx's
    public Python API (>=1.13.0) — no ctypes / C API binding.

    Because the model buffers internally, **output length does not match
    input length** — a call to :meth:`process` may return fewer samples than
    it was given, or an empty array while the model is still warming up.
    Callers should treat this as "what went in eventually comes out", not
    "chunk in, chunk out".
    """

    def __init__(self, model_path: str, *, sample_rate: int = 16000, num_threads: int = 1) -> None:
        import numpy as np
        import sherpa_onnx

        self._np = np
        config = sherpa_onnx.OnlineSpeechDenoiserConfig(
            model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(
                gtcrn=sherpa_onnx.OfflineSpeechDenoiserGtcrnModelConfig(model=model_path),
                num_threads=num_threads,
                debug=False,
                provider="cpu",
            )
        )
        if not config.validate():
            raise RuntimeError(f"invalid StreamingDenoiser config (check model_path={model_path!r})")
        self._denoiser = sherpa_onnx.OnlineSpeechDenoiser(config)
        self._sample_rate = sample_rate
        self._log = logging.getLogger(__name__)

    def process(self, chunk_int16: Any) -> Any:
        """Feed one mono int16 chunk in; return denoised mono int16 samples out.

        An empty input is passed through unchanged. Otherwise the chunk is
        converted to float32 in [-1, 1], run through the denoiser, and the
        result converted back to int16 (clipped). Output length need not
        match input length — see the class docstring.
        """
        np = self._np
        if len(chunk_int16) == 0:
            return chunk_int16
        x = chunk_int16.astype(np.float32) / 32768.0
        t0 = time.perf_counter()
        result = self._denoiser(x, self._sample_rate)
        infer_ms = (time.perf_counter() - t0) * 1000.0
        self._log.debug("streaming denoise: %.2f ms (%d samples in)", infer_ms, len(chunk_int16))
        return self._to_int16(result.samples)

    def flush(self) -> Any:
        """Flush the internal buffer, returning any remaining denoised samples."""
        result = self._denoiser.flush()
        return self._to_int16(result.samples)

    def stream(self, chunks: Iterable[Any]) -> Iterator[Any]:
        """Run :meth:`process` over each chunk, yielding only non-empty outputs.

        Meant to sit directly on a mic source, e.g.
        ``for out in denoiser.stream(mic.stream()): ...`` — note that's
        :meth:`MicStream.stream` (a ``Subscription``, the chunk source) feeding
        this method of the same name (``StreamingDenoiser.stream``, the chunk
        transform); the two are unrelated aside from sharing a name. Once
        ``chunks`` is exhausted, :meth:`flush` is called and its output is
        yielded too, if non-empty.
        """
        for chunk in chunks:
            out = self.process(chunk)
            if len(out) > 0:
                yield out
        tail = self.flush()
        if len(tail) > 0:
            yield tail

    def _to_int16(self, samples: Any) -> Any:
        np = self._np
        out = np.asarray(samples, dtype=np.float32)
        return np.clip(out * 32768.0, -32768, 32767).astype(np.int16)


__all__ = [
    "GTCRN_MODEL_SHA256",
    "GTCRN_MODEL_URL",
    "SpeechDenoiser",
    "StreamingDenoiser",
    "default_model_dir",
    "ensure_denoise_model",
]
