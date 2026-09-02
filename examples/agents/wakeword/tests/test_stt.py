"""Tests for :mod:`palmimo_wakeword_agent.stt` (no real OpenAI client, no network)."""

from __future__ import annotations

import wave
from typing import Any

import numpy as np

from palmimo_wakeword_agent.stt import TranscriptionLike, WhisperStt, encode_wav


def test_encode_wav_produces_parseable_mono_16bit_16k_wav() -> None:
    samples = np.array([0, 100, -100, 32767, -32768], dtype=np.int16)
    wav_bytes = encode_wav(samples, sample_rate=16000)

    import io

    with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == 16000
        assert reader.getnframes() == len(samples)
        frames = reader.readframes(reader.getnframes())
    decoded = np.frombuffer(frames, dtype=np.int16)
    assert np.array_equal(decoded, samples)


def test_encode_wav_default_sample_rate_is_16k() -> None:
    samples = np.zeros(10, dtype=np.int16)
    wav_bytes = encode_wav(samples)

    import io

    with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
        assert reader.getframerate() == 16000


class _FakeTranscription:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAudio:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[dict[str, Any]] = []

    class _Transcriptions:
        def __init__(self, outer: _FakeAudio) -> None:
            self._outer = outer

        def create(self, **kwargs: object) -> TranscriptionLike:
            self._outer.calls.append(kwargs)
            return _FakeTranscription(self._outer._text)

    @property
    def transcriptions(self) -> _FakeAudio._Transcriptions:
        return self._Transcriptions(self)


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.audio = _FakeAudio(text)


def test_whisper_stt_passes_model_language_and_named_file() -> None:
    client = _FakeClient("  こんにちは  ")
    stt = WhisperStt(client, model="gpt-4o-mini-transcribe", language="ja")

    result = stt(np.array([1, 2, 3], dtype=np.int16))

    assert result == "こんにちは"
    call = client.audio.calls[0]
    assert call["model"] == "gpt-4o-mini-transcribe"
    assert call["language"] == "ja"
    assert call["file"].name == "utterance.wav"


def test_whisper_stt_default_language_is_en() -> None:
    client = _FakeClient("hello")
    stt = WhisperStt(client)
    stt(np.array([0], dtype=np.int16))
    assert client.audio.calls[0]["language"] == "en"


def test_whisper_stt_strips_result_text() -> None:
    client = _FakeClient("踊ります\n")
    stt = WhisperStt(client)
    assert stt(np.array([0], dtype=np.int16)) == "踊ります"


def test_whisper_stt_default_model_is_gpt4o_mini_transcribe() -> None:
    client = _FakeClient("")
    stt = WhisperStt(client)
    stt(np.array([0], dtype=np.int16))
    assert client.audio.calls[0]["model"] == "gpt-4o-mini-transcribe"
