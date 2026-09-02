# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""AudioProcessor — a pluggable, DI-able WAV-in/WAV-out audio transform.

:class:`AudioProcessor` is the seam that lets a capture resource
(:class:`~palmimo_sdk.io.mic_stream.MicStream`,
:class:`~palmimo_sdk.io.microphone.Microphone`) push captured audio through an
arbitrary chain of transforms — denoising today, something else tomorrow —
without the capture layer knowing anything beyond "WAV bytes in, WAV bytes
out". :class:`Denoiser` is the bundled streaming implementation, wrapping
:class:`~palmimo_sdk.audio.denoise.StreamingDenoiser` behind that contract;
:class:`ClipDenoiser` is the offline counterpart for a complete, self-contained
clip, wrapping :class:`~palmimo_sdk.audio.denoise.SpeechDenoiser` instead.

:func:`int16_to_wav` / :func:`wav_to_int16` are the shared WAV <-> int16
ndarray helpers every processor (and the capture layer feeding them) uses;
they replace what used to be duplicated between ``mic_stream._to_wav_bytes``
and ``denoise.SpeechDenoiser.denoise_wav``'s inline WAV parsing.

Only the standard library (``wave``) is imported at module scope; ``numpy`` is
lazy-imported inside :func:`wav_to_int16` so importing this module — and
therefore ``AudioProcessor`` as a typing-only import — stays dependency-free
without the ``voice`` extra. :class:`Denoiser` additionally needs
``sherpa_onnx`` (lazy, via :class:`~palmimo_sdk.audio.denoise.StreamingDenoiser`)
and is only constructed, never merely imported, by dependency-free callers.
"""

from __future__ import annotations

import wave
from io import BytesIO
from typing import Any, Protocol


class AudioProcessor(Protocol):
    """A pluggable audio transform: mono 16k/S16_LE WAV bytes in, WAV bytes out.

    Implementations may be stateful (buffering internally, e.g. a streaming
    denoiser) — a single call to :meth:`process` is not required to return
    audio derived only from that call's input. Consequently the output may be
    shorter than the input, or empty (a valid, zero-frame WAV): callers must
    treat "what went in eventually comes out" as the contract, not "N frames
    in, N frames out". A stateless processor (e.g. a pure gain/filter) may of
    course always return exactly what it was given.

    Not part of the ``Protocol`` itself (structural typing can't require it),
    but by convention an implementation MAY expose a read-only ``sample_rate:
    int`` attribute naming the rate it requires — :class:`Denoiser` and
    :class:`ClipDenoiser` both do, and
    :meth:`~palmimo_sdk.io.mic_stream.MicStream.open` consults it (via
    ``getattr(processor, "sample_rate", None)``) to fail fast on a mismatch
    with the stream's own ``sample_rate`` instead of raising on every chunk.

    Likewise, an implementation MAY expose a read-only ``capture_channels:
    int`` attribute naming the number of interleaved input channels it
    requires in the WAV handed to :meth:`process` — :class:`~palmimo_sdk.audio.aec.EchoCanceller`
    does (it needs both a microphone channel and a loudspeaker-reference
    channel). Absent means 1 (mono), the contract every other processor here
    already assumes. The output of :meth:`process` is always mono WAV,
    regardless of ``capture_channels`` — only the FIRST processor in a
    ``processors`` chain may declare ``capture_channels > 1``; every later
    processor receives the previous one's mono output.
    :meth:`~palmimo_sdk.io.mic_stream.MicStream.open` validates this and
    raises (fail-fast) on violation.
    """

    def process(self, wav: bytes) -> bytes:
        """Transform *wav* (mono/16-bit WAV bytes), returning WAV bytes."""
        ...


def int16_to_wav(data: Any, sample_rate: int, channels: int = 1) -> bytes:
    """Encode an int16 ndarray as WAV bytes (16-bit / *sample_rate*).

    *data* must be C-contiguous: shape ``(frames,)`` for mono (the default,
    ``channels=1``) or ``(frames, channels)`` for multi-channel — ``.tobytes()``
    on a contiguous 2-D array already produces correctly interleaved bytes, so
    no reshaping or ``numpy`` import is needed here.
    """
    buf = BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)  # S16_LE
        writer.setframerate(sample_rate)
        writer.writeframes(data.tobytes())
    return buf.getvalue()


def wav_to_int16(wav: bytes) -> tuple[Any, int]:
    """Decode mono/16-bit WAV bytes to ``(int16 1-D ndarray, sample_rate)``.

    ``numpy`` is lazy-imported here so this module stays dependency-free to
    import without the ``voice`` extra.

    Raises:
        ValueError: *wav* is not mono, or not 16-bit.
    """
    import numpy as np  # lazy: keeps this module dependency-free to import

    with wave.open(BytesIO(wav), "rb") as reader:
        n_channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        frame_rate = reader.getframerate()
        n_frames = reader.getnframes()
        raw = reader.readframes(n_frames)
    if n_channels != 1:
        raise ValueError(f"expected mono WAV, got {n_channels} channels")
    if sample_width != 2:
        raise ValueError(f"expected 16-bit WAV, got {sample_width * 8}-bit")
    return np.frombuffer(raw, dtype=np.int16), frame_rate


def wav_to_int16_multi(wav: bytes) -> tuple[Any, int]:
    """Decode 16-bit WAV bytes of any channel count to ``(int16 2-D ndarray, sample_rate)``.

    Unlike :func:`wav_to_int16`, *wav* need not be mono — the returned array
    has shape ``(frames, channels)``. Used by processors (e.g.
    :class:`~palmimo_sdk.audio.aec.EchoCanceller`) that consume more than one
    interleaved channel (see the ``capture_channels`` convention on
    :class:`AudioProcessor`).

    Raises:
        ValueError: *wav* is not 16-bit.
    """
    import numpy as np  # lazy: keeps this module dependency-free to import

    with wave.open(BytesIO(wav), "rb") as reader:
        n_channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        frame_rate = reader.getframerate()
        n_frames = reader.getnframes()
        raw = reader.readframes(n_frames)
    if sample_width != 2:
        raise ValueError(f"expected 16-bit WAV, got {sample_width * 8}-bit")
    samples = np.frombuffer(raw, dtype=np.int16).reshape(-1, n_channels)
    return samples, frame_rate


class Denoiser:
    """:class:`AudioProcessor` implementation: GTCRN streaming denoise (denoise-first).

    Wraps :class:`~palmimo_sdk.audio.denoise.StreamingDenoiser` behind the
    WAV-in/WAV-out :class:`AudioProcessor` contract, so it can be dropped into
    :class:`~palmimo_sdk.io.mic_stream.MicStream`'s (or
    :class:`~palmimo_sdk.io.microphone.Microphone`'s) ``processors`` chain.

    All the heavy lifting — resolving/auto-downloading the GTCRN model via
    :func:`~palmimo_sdk.audio.denoise.ensure_denoise_model` and constructing
    the streaming denoiser (which lazy-imports ``sherpa_onnx`` / ``numpy``) —
    happens here in ``__init__``, matching ``MicStream.open``'s fail-fast
    contract: a caller that constructs a ``Denoiser`` up front finds out
    immediately if the model can't be resolved, instead of on the first chunk.

    Meant for a continuous stream (:class:`~palmimo_sdk.io.mic_stream.MicStream`):
    it is stateful and never flushed by its ``AudioProcessor`` callers, so
    using it on a self-contained clip cuts off whatever is still sitting in
    its internal buffer at the end, AND leaves that residue to bleed into the
    next clip's first :meth:`process` call. For a complete WAV clip (e.g.
    :class:`~palmimo_sdk.io.microphone.Microphone`'s recordings), use
    :class:`ClipDenoiser` instead, which denoises the whole clip in one
    stateless call.
    """

    #: Fixed capture rate this processor requires (GTCRN's rate). Read-only —
    #: consulted by :meth:`~palmimo_sdk.io.mic_stream.MicStream.open` to
    #: fail fast on a ``sample_rate`` mismatch instead of raising on every
    #: chunk from the capture thread (see :meth:`process`).
    sample_rate = 16000

    def __init__(self, model_path: str | None = None, *, num_threads: int = 1) -> None:
        from .denoise import StreamingDenoiser, ensure_denoise_model

        resolved_path = ensure_denoise_model(model_path)
        self._denoiser = StreamingDenoiser(resolved_path, sample_rate=self.sample_rate, num_threads=num_threads)

    def process(self, wav: bytes) -> bytes:
        """Denoise *wav*, returning WAV bytes (possibly 0 frames — see :class:`AudioProcessor`).

        Raises:
            ValueError: *wav*'s sample rate does not match this instance's
                configured rate (16000 Hz).
        """
        samples, sample_rate = wav_to_int16(wav)
        if sample_rate != self.sample_rate:
            raise ValueError(f"Denoiser expects {self.sample_rate} Hz WAV, got {sample_rate} Hz")
        denoised = self._denoiser.process(samples)
        return int16_to_wav(denoised, self.sample_rate)


class ClipDenoiser:
    """:class:`AudioProcessor` implementation: GTCRN offline denoise for a complete clip.

    Wraps :class:`~palmimo_sdk.audio.denoise.SpeechDenoiser` (the offline/batch
    GTCRN engine) behind the WAV-in/WAV-out :class:`AudioProcessor` contract —
    the counterpart to :class:`Denoiser` for a self-contained recording rather
    than a continuous stream. Meant for
    :class:`~palmimo_sdk.io.microphone.Microphone`'s ``processors`` chain
    (``processors=[ClipDenoiser()]``): each :meth:`process` call denoises the
    whole WAV it's given in one shot and carries no state between calls, so
    unlike :class:`Denoiser` there is no tail cut off and no residue to bleed
    into the next clip.

    Model resolution/download and construction happen in ``__init__``, same
    fail-fast contract as :class:`Denoiser`.
    """

    #: Fixed capture rate this processor requires (GTCRN's rate); see
    #: :attr:`Denoiser.sample_rate`.
    sample_rate = 16000

    def __init__(self, model_path: str | None = None, *, num_threads: int = 1) -> None:
        from .denoise import SpeechDenoiser, ensure_denoise_model

        resolved_path = ensure_denoise_model(model_path)
        self._denoiser = SpeechDenoiser(resolved_path, sample_rate=self.sample_rate, num_threads=num_threads)

    def process(self, wav: bytes) -> bytes:
        """Denoise the entirety of *wav* in one stateless call, returning WAV bytes.

        Raises:
            ValueError: *wav*'s sample rate does not match this instance's
                configured rate (16000 Hz).
        """
        samples, sample_rate = wav_to_int16(wav)
        if sample_rate != self.sample_rate:
            raise ValueError(f"ClipDenoiser expects {self.sample_rate} Hz WAV, got {sample_rate} Hz")
        denoised = self._denoiser.denoise(samples)
        return int16_to_wav(denoised, self.sample_rate)


__all__ = [
    "AudioProcessor",
    "ClipDenoiser",
    "Denoiser",
    "int16_to_wav",
    "wav_to_int16",
    "wav_to_int16_multi",
]
