"""Transcription via OpenAI's Whisper-family transcription API.

Used for every VAD-segmented utterance: both wake-word detection and the
actual command text go through the same remote model here.
"""

from __future__ import annotations

import wave
from io import BytesIO
from typing import IO, Protocol

import numpy as np

from .vad import Int16Array


def encode_wav(samples: Int16Array, sample_rate: int = 16000) -> bytes:
    """Encode a mono int16 *samples* array as WAV bytes (16-bit / mono / *sample_rate*)."""
    buf = BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(samples.astype(np.int16).tobytes())
    return buf.getvalue()


class TranscriptionLike(Protocol):
    """Minimal structural surface of an OpenAI transcription response."""

    text: str


class _TranscriptionsLike(Protocol):
    def create(self, *, model: str, file: IO[bytes], language: str) -> TranscriptionLike: ...


class _AudioLike(Protocol):
    @property
    def transcriptions(self) -> _TranscriptionsLike: ...


class TranscriptionClientLike(Protocol):
    """Minimal structural surface of an ``openai.OpenAI``-compatible client we depend on."""

    @property
    def audio(self) -> _AudioLike: ...


class WhisperStt:
    """OpenAI Whisper-family transcription over one utterance.

    Args:
        client: An ``openai.OpenAI``-compatible object (dependency-injected
            for tests).
        model: Transcription model name.
        language: ISO 639-1 language code passed to Whisper as a transcription
            hint (e.g. "en", "ja").
    """

    def __init__(
        self,
        client: TranscriptionClientLike,
        model: str = "gpt-4o-mini-transcribe",
        language: str = "en",
    ) -> None:
        self._client = client
        self._model = model
        self._language = language

    def __call__(self, utterance: Int16Array) -> str:
        """Transcribe one int16 *utterance* array; return stripped text."""
        wav_bytes = encode_wav(utterance)
        named_file = BytesIO(wav_bytes)
        named_file.name = "utterance.wav"
        result = self._client.audio.transcriptions.create(
            model=self._model,
            file=named_file,
            language=self._language,
        )
        return result.text.strip()
