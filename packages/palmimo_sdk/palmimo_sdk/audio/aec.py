# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""EchoCanceller — DTLN-AEC behind the AudioProcessor contract.

Wraps :class:`~palmimo_sdk.audio.dtln.GatedDtlnCanceller` as an
:class:`~palmimo_sdk.audio.processor.AudioProcessor`, the same shape
:class:`~palmimo_sdk.audio.processor.Denoiser` uses, so it can be dropped
into :class:`~palmimo_sdk.io.mic_stream.MicStream`'s ``processors`` chain.
Unlike ``Denoiser``, it consumes a multi-channel WAV (microphone + loudspeaker
reference) and produces mono — see :class:`EchoCanceller`'s ``capture_channels``.
"""

from __future__ import annotations

from .dtln import DEFAULT_SIZE, FAR_END_SILENCE_DB, HANGOVER_HOPS
from .processor import int16_to_wav, wav_to_int16_multi


class EchoCanceller:
    """:class:`~palmimo_sdk.audio.processor.AudioProcessor` implementation: DTLN-AEC.

    Cancels the loudspeaker only: servo/motor noise has no reference channel
    and passes through untouched. This processor requires a capture device
    that exposes the loudspeaker loopback as an extra channel — ReSpeaker-family
    XMOS arrays do this natively. On a device without enough channels,
    :meth:`~palmimo_sdk.io.mic_stream.MicStream.open` fails; the remedy is
    ``processors=[Denoiser()]`` (GTCRN noise-only) or ``processors=[]`` (raw).

    All the heavy lifting — resolving/auto-downloading both DTLN-AEC tflite
    models and building the interpreters — happens in ``__init__``, matching
    :class:`~palmimo_sdk.audio.processor.Denoiser`'s fail-fast contract.

    Attributes:
        capture_channels: Number of interleaved channels this processor
            requires in the WAV handed to :meth:`process` — reflects the
            configured ``channels`` argument (unlike :attr:`sample_rate`,
            which is fixed).
    """

    #: Fixed capture rate this processor requires (DTLN's rate); see
    #: :attr:`~palmimo_sdk.audio.processor.Denoiser.sample_rate`.
    sample_rate = 16000

    def __init__(
        self,
        model_dir: str | None = None,
        *,
        size: int = DEFAULT_SIZE,
        channels: int = 6,
        near_channel: int = 1,
        reference_channel: int = 5,
        far_end_silence_db: float = FAR_END_SILENCE_DB,
        hangover_hops: int = HANGOVER_HOPS,
    ) -> None:
        """Resolve the models and build the canceller.

        The defaults (``channels=6, near_channel=1, reference_channel=5``)
        are not arbitrary — they match a ReSpeaker/XMOS-family array's
        channel layout: channel 0 is the chip's OWN processed output (its
        own gain and suppression already applied), which a canceller must
        not use as "near" because it can't see what was already done to it;
        channels 1-4 are the raw, unprocessed capsules (channel 1 is the one
        used here); channel 5 is the loudspeaker loopback — the far-end
        reference this canceller needs.

        Args:
            model_dir: Cache directory for the DTLN-AEC models; None uses the
                shared model cache.
            size: DTLN-AEC model width to load.
            channels: Interleaved channels the capture device opens.
            near_channel: Microphone channel index to clean and return (see
                the channel-layout note above for why this defaults to 1,
                not 0).
            reference_channel: Loudspeaker loopback channel index (see the
                channel-layout note above for why this defaults to 5).
            far_end_silence_db: Reference level below which the loudspeaker
                counts as silent.
            hangover_hops: Hops to stay engaged after the reference falls quiet.

        Raises:
            ValueError: *near_channel* / *reference_channel* are outside
                ``[0, channels)``, or are the same channel.
        """
        for name, index in (("near_channel", near_channel), ("reference_channel", reference_channel)):
            if not 0 <= index < channels:
                raise ValueError(f"{name}={index} is outside the {channels} channels opened")
        if near_channel == reference_channel:
            raise ValueError(f"near_channel and reference_channel are both {near_channel}")

        from .dtln import DtlnResidual, GatedDtlnCanceller  # lazy: keeps `import palmimo_sdk` dependency-free

        self.capture_channels = channels
        self.near_channel = near_channel
        self.reference_channel = reference_channel
        residual = DtlnResidual.load(size, model_dir, sample_rate=self.sample_rate)
        self._canceller = GatedDtlnCanceller(
            residual=residual, far_end_silence_db=far_end_silence_db, hangover_hops=hangover_hops
        )

    def process(self, wav: bytes) -> bytes:
        """Cancel the loudspeaker out of the near channel, returning mono WAV bytes.

        Raises:
            ValueError: *wav*'s sample rate does not match this instance's
                configured rate (16000 Hz), or its channel count does not
                match :attr:`capture_channels`.
        """
        samples, sample_rate = wav_to_int16_multi(wav)
        if sample_rate != self.sample_rate:
            raise ValueError(f"EchoCanceller expects {self.sample_rate} Hz WAV, got {sample_rate} Hz")
        got_channels = samples.shape[1]
        if got_channels != self.capture_channels:
            raise ValueError(f"EchoCanceller expects {self.capture_channels} channels, got {got_channels}")
        near = samples[:, self.near_channel]
        reference = samples[:, self.reference_channel]
        cleaned = self._canceller.process(near, reference)
        return int16_to_wav(cleaned, self.sample_rate)


__all__ = ["EchoCanceller"]
