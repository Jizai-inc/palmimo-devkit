"""Tests for palmimo_sdk.audio.processor — AudioProcessor protocol, WAV helpers, Denoiser.

``sherpa_onnx`` is assumed absent from the test environment: a fake module
(mirroring the small ``OnlineSpeechDenoiser*`` slice of the ``sherpa_onnx`` API
that :class:`~palmimo_sdk.audio.denoise.StreamingDenoiser` calls) is injected
via ``monkeypatch.setitem(sys.modules, ...)`` before constructing
:class:`~palmimo_sdk.audio.processor.Denoiser`, mirroring ``tests/test_denoise.py``.
``ensure_denoise_model`` is monkeypatched too, so no network access / real
model file is needed. ``numpy`` is real (already a base test dependency).
"""

from __future__ import annotations

import sys
import types
import wave
from io import BytesIO
from typing import Any

import numpy as np
import pytest

from palmimo_sdk.audio.processor import AudioProcessor, ClipDenoiser, Denoiser, int16_to_wav, wav_to_int16


# ----------------------------------------------------------------------
# int16_to_wav / wav_to_int16
# ----------------------------------------------------------------------


def test_int16_to_wav_wav_to_int16_round_trip_preserves_samples_and_sample_rate() -> None:
    samples = np.array([0, 1000, -1000, 32767, -32768], dtype=np.int16)

    wav = int16_to_wav(samples, 16000)
    out_samples, out_sample_rate = wav_to_int16(wav)

    np.testing.assert_array_equal(out_samples, samples)
    assert out_sample_rate == 16000


def test_int16_to_wav_round_trip_with_empty_array_yields_zero_frame_wav() -> None:
    samples = np.array([], dtype=np.int16)

    wav = int16_to_wav(samples, 16000)
    out_samples, out_sample_rate = wav_to_int16(wav)

    assert len(out_samples) == 0
    assert out_sample_rate == 16000
    with wave.open(BytesIO(wav), "rb") as reader:
        assert reader.getnframes() == 0


def test_wav_to_int16_raises_value_error_for_stereo() -> None:
    buf = BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(np.array([0, 1, 2, 3], dtype=np.int16).tobytes())

    with pytest.raises(ValueError, match="mono"):
        wav_to_int16(buf.getvalue())


def test_wav_to_int16_raises_value_error_for_wrong_sample_width() -> None:
    buf = BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(1)  # 8-bit
        writer.setframerate(16000)
        writer.writeframes(bytes([0, 1, 2, 3]))

    with pytest.raises(ValueError, match="16-bit"):
        wav_to_int16(buf.getvalue())


def test_audio_processor_is_a_runtime_structural_protocol() -> None:
    class _Passthrough:
        def process(self, wav: bytes) -> bytes:
            return wav

    processor: AudioProcessor = _Passthrough()
    assert processor.process(b"anything") == b"anything"


# ----------------------------------------------------------------------
# fake sherpa_onnx module (StreamingDenoiser's OnlineSpeechDenoiser slice only)
# ----------------------------------------------------------------------


class _FakeGtcrnModelConfig:
    def __init__(self, model: str) -> None:
        self.model = model


class _FakeOfflineSpeechDenoiserModelConfig:
    def __init__(self, *, gtcrn: Any, num_threads: int, debug: bool, provider: str) -> None:
        self.gtcrn = gtcrn
        self.num_threads = num_threads
        self.debug = debug
        self.provider = provider


class _FakeOnlineSpeechDenoiserConfig:
    def __init__(self, *, model: Any) -> None:
        self.model = model

    def validate(self) -> bool:
        return True


class _FakeResult:
    def __init__(self, samples: Any, sample_rate: int) -> None:
        self.samples = samples
        self.sample_rate = sample_rate


class _FakeOnlineSpeechDenoiser:
    """Stand-in for sherpa_onnx.OnlineSpeechDenoiser: scales samples by *scale*.

    ``process_outputs``, if set, overrides the scale-based output for
    successive calls (used to simulate empty/buffering output).
    """

    def __init__(self, config: _FakeOnlineSpeechDenoiserConfig | None, *, scale: float = 1.0) -> None:
        self.config = config
        self.scale = scale
        self.process_outputs: list[list[float]] | None = None
        self.process_calls: list[tuple[Any, int]] = []
        self.flush_calls = 0

    def __call__(self, samples: Any, sample_rate: int) -> _FakeResult:
        self.process_calls.append((np.asarray(samples).copy(), sample_rate))
        if self.process_outputs is not None:
            values = self.process_outputs.pop(0) if self.process_outputs else []
            return _FakeResult(np.asarray(values, dtype=np.float32), sample_rate)
        return _FakeResult(np.asarray(samples) * self.scale, sample_rate)

    def flush(self) -> _FakeResult:
        self.flush_calls += 1
        return _FakeResult(np.asarray([], dtype=np.float32), 16000)


class _FakeOfflineSpeechDenoiserConfig:
    def __init__(self, *, model: Any) -> None:
        self.model = model


class _FakeOfflineRunResult:
    def __init__(self, samples: Any, sample_rate: int) -> None:
        self.samples = samples
        self.sample_rate = sample_rate


class _FakeOfflineSpeechDenoiser:
    """Stand-in for sherpa_onnx.OfflineSpeechDenoiser: scales samples by *scale*, records calls."""

    def __init__(self, config: _FakeOfflineSpeechDenoiserConfig | None, *, scale: float = 1.0) -> None:
        self.config = config
        self.scale = scale
        self.run_calls: list[Any] = []

    def run(self, samples: Any, sample_rate: int) -> _FakeOfflineRunResult:
        self.run_calls.append(np.asarray(samples).copy())
        return _FakeOfflineRunResult(np.asarray(samples) * self.scale, sample_rate)


def _make_fake_sherpa_onnx_module(
    online_denoiser: _FakeOnlineSpeechDenoiser | None = None,
    *,
    offline_denoiser: _FakeOfflineSpeechDenoiser | None = None,
) -> types.ModuleType:
    module = types.ModuleType("sherpa_onnx")
    module.OfflineSpeechDenoiserGtcrnModelConfig = _FakeGtcrnModelConfig  # type: ignore[attr-defined]
    module.OfflineSpeechDenoiserModelConfig = _FakeOfflineSpeechDenoiserModelConfig  # type: ignore[attr-defined]
    module.OnlineSpeechDenoiserConfig = _FakeOnlineSpeechDenoiserConfig  # type: ignore[attr-defined]
    module.OnlineSpeechDenoiser = lambda config: online_denoiser  # type: ignore[attr-defined]
    module.OfflineSpeechDenoiserConfig = _FakeOfflineSpeechDenoiserConfig  # type: ignore[attr-defined]
    module.OfflineSpeechDenoiser = lambda config: offline_denoiser  # type: ignore[attr-defined]
    return module


def _patch_ensure_denoise_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("palmimo_sdk.audio.denoise.ensure_denoise_model", lambda path=None: "/fake/model.onnx")


# ----------------------------------------------------------------------
# Denoiser
# ----------------------------------------------------------------------


def test_denoiser_process_round_trips_wav_bytes_in_and_out(monkeypatch: pytest.MonkeyPatch) -> None:
    online = _FakeOnlineSpeechDenoiser(config=None, scale=1.0)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _make_fake_sherpa_onnx_module(online))
    _patch_ensure_denoise_model(monkeypatch)

    denoiser = Denoiser()
    samples = np.array([0, 1000, -1000, 5000], dtype=np.int16)
    wav_in = int16_to_wav(samples, 16000)

    wav_out = denoiser.process(wav_in)

    assert isinstance(wav_out, bytes)
    out_samples, out_sample_rate = wav_to_int16(wav_out)
    assert out_sample_rate == 16000
    np.testing.assert_allclose(out_samples, samples, atol=2)


def test_denoiser_process_returns_zero_frame_wav_when_output_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    online = _FakeOnlineSpeechDenoiser(config=None)
    online.process_outputs = [[]]  # simulate the streaming denoiser still buffering
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _make_fake_sherpa_onnx_module(online))
    _patch_ensure_denoise_model(monkeypatch)

    denoiser = Denoiser()
    wav_in = int16_to_wav(np.zeros(160, dtype=np.int16), 16000)

    wav_out = denoiser.process(wav_in)

    out_samples, _sr = wav_to_int16(wav_out)
    assert len(out_samples) == 0
    with wave.open(BytesIO(wav_out), "rb") as reader:
        assert reader.getnframes() == 0


def test_denoiser_process_raises_value_error_on_sample_rate_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    online = _FakeOnlineSpeechDenoiser(config=None)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _make_fake_sherpa_onnx_module(online))
    _patch_ensure_denoise_model(monkeypatch)

    denoiser = Denoiser()
    wav_in = int16_to_wav(np.array([0, 1, 2], dtype=np.int16), 8000)

    with pytest.raises(ValueError, match="16000"):
        denoiser.process(wav_in)


def test_denoiser_init_resolves_model_path_via_ensure_denoise_model(monkeypatch: pytest.MonkeyPatch) -> None:
    online = _FakeOnlineSpeechDenoiser(config=None)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _make_fake_sherpa_onnx_module(online))
    calls: list[str | None] = []

    def fake_ensure(path: str | None = None) -> str:
        calls.append(path)
        return "/resolved/model.onnx"

    monkeypatch.setattr("palmimo_sdk.audio.denoise.ensure_denoise_model", fake_ensure)

    Denoiser("/custom/model.onnx")

    assert calls == ["/custom/model.onnx"]


def test_denoiser_exposes_a_read_only_16000_sample_rate_class_attribute() -> None:
    # No sherpa_onnx / model needed: this is a class-level attribute, not
    # something __init__ computes — MicStream.open() reads it (via getattr)
    # before ever constructing a Denoiser (see test_mic_stream.py).
    assert Denoiser.sample_rate == 16000


# ----------------------------------------------------------------------
# ClipDenoiser
# ----------------------------------------------------------------------


def test_clip_denoiser_exposes_a_read_only_16000_sample_rate_class_attribute() -> None:
    assert ClipDenoiser.sample_rate == 16000


def test_clip_denoiser_process_round_trips_wav_bytes_using_the_offline_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offline = _FakeOfflineSpeechDenoiser(config=None, scale=1.0)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _make_fake_sherpa_onnx_module(offline_denoiser=offline))
    _patch_ensure_denoise_model(monkeypatch)

    denoiser = ClipDenoiser()
    samples = np.array([0, 1000, -1000, 5000], dtype=np.int16)
    wav_in = int16_to_wav(samples, 16000)

    wav_out = denoiser.process(wav_in)

    assert isinstance(wav_out, bytes)
    out_samples, out_sample_rate = wav_to_int16(wav_out)
    assert out_sample_rate == 16000
    np.testing.assert_allclose(out_samples, samples, atol=2)
    # SpeechDenoiser.denoise() calls the offline engine's .run() once — a
    # single, whole-clip call, not a streaming/chunked one.
    assert len(offline.run_calls) == 1


def test_clip_denoiser_process_has_no_state_across_two_consecutive_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two back-to-back clips must be denoised independently (no carry-over).

    Unlike the streaming ``Denoiser``, ``ClipDenoiser`` wraps the *offline*
    engine: every ``process()`` call is a fresh, self-contained ``run()`` over
    exactly what it was given — nothing buffered from the previous call
    leaks in, and nothing from this call is held back for the next one.
    """
    offline = _FakeOfflineSpeechDenoiser(config=None, scale=2.0)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _make_fake_sherpa_onnx_module(offline_denoiser=offline))
    _patch_ensure_denoise_model(monkeypatch)

    denoiser = ClipDenoiser()
    first = np.array([1, 2, 3], dtype=np.int16)
    second = np.array([10, 20, 30], dtype=np.int16)

    out1 = denoiser.process(int16_to_wav(first, 16000))
    out2 = denoiser.process(int16_to_wav(second, 16000))

    samples1, _ = wav_to_int16(out1)
    samples2, _ = wav_to_int16(out2)
    # Each call's output derives only from that call's own input (scale=2),
    # not from anything left over from the other call.
    np.testing.assert_allclose(samples1, first * 2, atol=1)
    np.testing.assert_allclose(samples2, second * 2, atol=1)
    assert len(offline.run_calls) == 2
    # SpeechDenoiser.denoise() normalizes to float32 in [-1, 1] before run().
    np.testing.assert_allclose(offline.run_calls[0], first.astype(np.float32) / 32768.0, atol=1e-6)
    np.testing.assert_allclose(offline.run_calls[1], second.astype(np.float32) / 32768.0, atol=1e-6)


def test_clip_denoiser_process_raises_value_error_on_sample_rate_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offline = _FakeOfflineSpeechDenoiser(config=None)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _make_fake_sherpa_onnx_module(offline_denoiser=offline))
    _patch_ensure_denoise_model(monkeypatch)

    denoiser = ClipDenoiser()
    wav_in = int16_to_wav(np.array([0, 1, 2], dtype=np.int16), 8000)

    with pytest.raises(ValueError, match="16000"):
        denoiser.process(wav_in)
