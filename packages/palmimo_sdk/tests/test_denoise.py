"""Tests for palmimo_sdk.audio.denoise — GTCRN noise removal.

``sherpa_onnx`` is assumed absent from the test environment: a fake module
(``SimpleNamespace``-based, mirroring the ``sherpa_onnx`` API surface
``denoise.py`` calls) is injected via ``monkeypatch.setitem(sys.modules, ...)``
before constructing :class:`SpeechDenoiser` / :class:`StreamingDenoiser`, so
construction runs the real ``__init__`` wiring against the fake. ``numpy`` is
real (it is a base test dependency already).
"""

from __future__ import annotations

import sys
import types
import wave
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from palmimo_sdk.audio.denoise import (
    GTCRN_MODEL_SHA256,
    GTCRN_MODEL_URL,
    SpeechDenoiser,
    StreamingDenoiser,
    default_model_dir,
    ensure_denoise_model,
)


# ----------------------------------------------------------------------
# fake sherpa_onnx module
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


class _FakeOfflineSpeechDenoiserConfig:
    def __init__(self, *, model: Any) -> None:
        self.model = model


class _FakeRunResult:
    def __init__(self, samples: Any, sample_rate: int) -> None:
        self.samples = samples
        self.sample_rate = sample_rate


class _FakeOfflineSpeechDenoiser:
    """Stand-in for sherpa_onnx.OfflineSpeechDenoiser; scales samples by *scale*."""

    def __init__(
        self, config: _FakeOfflineSpeechDenoiserConfig | None, *, scale: float = 1.0, out_sample_rate: int | None = None
    ) -> None:
        self.config = config
        self.scale = scale
        self.out_sample_rate = out_sample_rate
        self.run_calls: list[tuple[Any, int]] = []

    def run(self, samples: Any, sample_rate: int) -> _FakeRunResult:
        self.run_calls.append((samples, sample_rate))
        out_sr = self.out_sample_rate if self.out_sample_rate is not None else sample_rate
        return _FakeRunResult(np.asarray(samples) * self.scale, out_sr)


class _FakeOnlineSpeechDenoiserConfig:
    def __init__(self, *, model: Any) -> None:
        self.model = model
        self.valid = True

    def validate(self) -> bool:
        return self.valid


class _FakeOnlineSpeechDenoiser:
    """Stand-in for sherpa_onnx.OnlineSpeechDenoiser; callable, queued outputs."""

    def __init__(self, config: _FakeOnlineSpeechDenoiserConfig | None) -> None:
        self.config = config
        self.process_calls: list[tuple[Any, int]] = []
        self.process_outputs: list[list[float]] = []
        self.flush_output: list[float] = []
        self.flush_calls = 0

    def __call__(self, samples: Any, sample_rate: int) -> _FakeRunResult:
        self.process_calls.append((np.asarray(samples).copy(), sample_rate))
        values = self.process_outputs.pop(0) if self.process_outputs else []
        return _FakeRunResult(np.asarray(values, dtype=np.float32), sample_rate)

    def flush(self) -> _FakeRunResult:
        self.flush_calls += 1
        return _FakeRunResult(np.asarray(self.flush_output, dtype=np.float32), 16000)


def _make_fake_sherpa_onnx_module(
    *,
    offline_denoiser: _FakeOfflineSpeechDenoiser | None = None,
    online_denoiser: _FakeOnlineSpeechDenoiser | None = None,
) -> types.ModuleType:
    module = types.ModuleType("sherpa_onnx")
    module.OfflineSpeechDenoiserGtcrnModelConfig = _FakeGtcrnModelConfig  # type: ignore[attr-defined]
    module.OfflineSpeechDenoiserModelConfig = _FakeOfflineSpeechDenoiserModelConfig  # type: ignore[attr-defined]
    module.OfflineSpeechDenoiserConfig = _FakeOfflineSpeechDenoiserConfig  # type: ignore[attr-defined]
    module.OfflineSpeechDenoiser = (  # type: ignore[attr-defined]
        (lambda config: offline_denoiser) if offline_denoiser is not None else _FakeOfflineSpeechDenoiser
    )
    module.OnlineSpeechDenoiserConfig = _FakeOnlineSpeechDenoiserConfig  # type: ignore[attr-defined]
    module.OnlineSpeechDenoiser = (  # type: ignore[attr-defined]
        (lambda config: online_denoiser) if online_denoiser is not None else _FakeOnlineSpeechDenoiser
    )
    return module


# ----------------------------------------------------------------------
# ensure_denoise_model
# ----------------------------------------------------------------------


def _patch_download(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_download_atomic(url: str, dest: Any, *, timeout: float = 30.0, sha256: str | None = None) -> None:
        calls.append({"url": url, "dest": str(dest), "timeout": timeout, "sha256": sha256})
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"fake model bytes")

    monkeypatch.setattr("palmimo_sdk.audio.denoise.download_atomic", fake_download_atomic)
    return calls


def test_ensure_denoise_model_skips_download_when_file_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    existing = tmp_path / "gtcrn_simple.onnx"
    existing.write_bytes(b"already here")
    calls = _patch_download(monkeypatch)

    result = ensure_denoise_model(str(existing))

    assert result == str(existing)
    assert calls == []


def test_ensure_denoise_model_downloads_with_correct_url_and_sha256_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "gtcrn_simple.onnx"
    calls = _patch_download(monkeypatch)

    result = ensure_denoise_model(str(dest))

    assert result == str(dest)
    assert calls == [{"url": GTCRN_MODEL_URL, "dest": str(dest), "timeout": 60.0, "sha256": GTCRN_MODEL_SHA256}]


def test_ensure_denoise_model_wraps_download_failure_in_runtime_error_with_offline_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "gtcrn_simple.onnx"

    def failing_download(*args: Any, **kwargs: Any) -> None:
        raise OSError("network unreachable")

    monkeypatch.setattr("palmimo_sdk.audio.denoise.download_atomic", failing_download)

    with pytest.raises(RuntimeError, match="offline") as excinfo:
        ensure_denoise_model(str(dest))

    assert "network unreachable" in str(excinfo.value)
    assert "path" in str(excinfo.value)


def test_ensure_denoise_model_respects_xdg_cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    calls = _patch_download(monkeypatch)

    result = ensure_denoise_model()

    expected = tmp_path / "palmimo" / "models" / "gtcrn_simple.onnx"
    assert result == str(expected)
    assert calls[0]["dest"] == str(expected)


def test_default_model_dir_falls_back_to_cache_home_when_xdg_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert default_model_dir() == tmp_path / ".cache" / "palmimo" / "models"


def test_ensure_denoise_model_uses_explicit_path_and_expands_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = _patch_download(monkeypatch)

    result = ensure_denoise_model("~/models/custom.onnx")

    expected = tmp_path / "models" / "custom.onnx"
    assert result == str(expected)
    assert calls[0]["dest"] == str(expected)


# ----------------------------------------------------------------------
# SpeechDenoiser.denoise
# ----------------------------------------------------------------------


def test_speech_denoiser_denoise_round_trips_int16(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_denoiser = _FakeOfflineSpeechDenoiser(config=None, scale=1.0)
    fake_module = _make_fake_sherpa_onnx_module(offline_denoiser=fake_denoiser)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_module)

    denoiser = SpeechDenoiser("/fake/model.onnx", sample_rate=16000)
    samples = np.array([0, 100, -100, 32767, -32768], dtype=np.int16)

    out = denoiser.denoise(samples)

    assert out.dtype == np.int16
    np.testing.assert_allclose(out, samples, atol=2)
    fed_samples, fed_sr = fake_denoiser.run_calls[0]
    assert fed_sr == 16000
    assert fed_samples.dtype == np.float32
    np.testing.assert_allclose(fed_samples, samples.astype(np.float32) / 32768.0)


def test_speech_denoiser_denoise_passes_through_empty_array_without_calling_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_denoiser = _FakeOfflineSpeechDenoiser(config=None)
    fake_module = _make_fake_sherpa_onnx_module(offline_denoiser=fake_denoiser)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_module)

    denoiser = SpeechDenoiser("/fake/model.onnx")
    samples = np.array([], dtype=np.int16)

    out = denoiser.denoise(samples)

    assert len(out) == 0
    assert fake_denoiser.run_calls == []


def test_speech_denoiser_denoise_returns_original_when_sample_rate_mismatches(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake_denoiser = _FakeOfflineSpeechDenoiser(config=None, out_sample_rate=8000)
    fake_module = _make_fake_sherpa_onnx_module(offline_denoiser=fake_denoiser)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_module)

    denoiser = SpeechDenoiser("/fake/model.onnx", sample_rate=16000)
    samples = np.array([1, 2, 3], dtype=np.int16)

    with caplog.at_level("WARNING"):
        out = denoiser.denoise(samples)

    np.testing.assert_array_equal(out, samples)
    assert any("sample_rate" in r.message for r in caplog.records)


# ----------------------------------------------------------------------
# SpeechDenoiser.denoise_wav
# ----------------------------------------------------------------------


def _make_wav_bytes(samples: Any, *, sample_rate: int = 16000, channels: int = 1, sample_width: int = 2) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(np.asarray(samples, dtype=np.int16).tobytes())
    return buf.getvalue()


def test_denoise_wav_round_trips_valid_mono_16bit_wav(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_denoiser = _FakeOfflineSpeechDenoiser(config=None, scale=1.0)
    fake_module = _make_fake_sherpa_onnx_module(offline_denoiser=fake_denoiser)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_module)

    denoiser = SpeechDenoiser("/fake/model.onnx", sample_rate=16000)
    samples = np.array([0, 1000, -1000, 5000], dtype=np.int16)
    wav_in = _make_wav_bytes(samples, sample_rate=16000)

    wav_out = denoiser.denoise_wav(wav_in)

    with wave.open(BytesIO(wav_out), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == 16000
        out_samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype=np.int16)
    np.testing.assert_allclose(out_samples, samples, atol=2)


def test_denoise_wav_raises_value_error_for_stereo(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_module = _make_fake_sherpa_onnx_module(offline_denoiser=_FakeOfflineSpeechDenoiser(config=None))
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_module)
    denoiser = SpeechDenoiser("/fake/model.onnx", sample_rate=16000)
    wav_in = _make_wav_bytes(np.array([0, 1, 2, 3], dtype=np.int16), channels=2)

    with pytest.raises(ValueError, match="mono"):
        denoiser.denoise_wav(wav_in)


def test_denoise_wav_raises_value_error_for_wrong_sample_width(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_module = _make_fake_sherpa_onnx_module(offline_denoiser=_FakeOfflineSpeechDenoiser(config=None))
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_module)
    denoiser = SpeechDenoiser("/fake/model.onnx", sample_rate=16000)
    wav_in = _make_wav_bytes(np.array([0, 1], dtype=np.int16), sample_width=1)

    with pytest.raises(ValueError, match="16-bit"):
        denoiser.denoise_wav(wav_in)


def test_denoise_wav_raises_value_error_for_mismatched_sample_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_module = _make_fake_sherpa_onnx_module(offline_denoiser=_FakeOfflineSpeechDenoiser(config=None))
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_module)
    denoiser = SpeechDenoiser("/fake/model.onnx", sample_rate=16000)
    wav_in = _make_wav_bytes(np.array([0, 1], dtype=np.int16), sample_rate=8000)

    with pytest.raises(ValueError, match="Hz"):
        denoiser.denoise_wav(wav_in)


# ----------------------------------------------------------------------
# StreamingDenoiser
# ----------------------------------------------------------------------


def test_streaming_denoiser_process_converts_int16_to_float32_and_back(monkeypatch: pytest.MonkeyPatch) -> None:
    online = _FakeOnlineSpeechDenoiser(config=None)
    online.process_outputs = [[0.5, -0.25]]
    fake_module = _make_fake_sherpa_onnx_module(online_denoiser=online)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_module)

    denoiser = StreamingDenoiser("/fake/model.onnx", sample_rate=16000)
    out = denoiser.process(np.array([16384, -8192, 0], dtype=np.int16))

    fed_samples, fed_sr = online.process_calls[0]
    assert fed_sr == 16000
    np.testing.assert_allclose(fed_samples, [0.5, -0.25, 0.0])
    assert out.dtype == np.int16
    np.testing.assert_array_equal(out, [16384, -8192])


def test_streaming_denoiser_process_returns_empty_array_for_empty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    online = _FakeOnlineSpeechDenoiser(config=None)
    fake_module = _make_fake_sherpa_onnx_module(online_denoiser=online)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_module)

    denoiser = StreamingDenoiser("/fake/model.onnx")
    out = denoiser.process(np.array([], dtype=np.int16))

    assert len(out) == 0
    assert online.process_calls == []


def test_streaming_denoiser_process_returns_empty_while_buffering(monkeypatch: pytest.MonkeyPatch) -> None:
    online = _FakeOnlineSpeechDenoiser(config=None)
    online.process_outputs = [[]]
    fake_module = _make_fake_sherpa_onnx_module(online_denoiser=online)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_module)

    denoiser = StreamingDenoiser("/fake/model.onnx")
    out = denoiser.process(np.zeros(160, dtype=np.int16))

    assert out.dtype == np.int16
    assert len(out) == 0


def test_streaming_denoiser_flush_drains_remaining_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    online = _FakeOnlineSpeechDenoiser(config=None)
    online.flush_output = [0.25]
    fake_module = _make_fake_sherpa_onnx_module(online_denoiser=online)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_module)

    denoiser = StreamingDenoiser("/fake/model.onnx")
    out = denoiser.flush()

    assert online.flush_calls == 1
    np.testing.assert_array_equal(out, [8192])


def test_streaming_denoiser_stream_yields_only_nonempty_and_includes_flush_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    online = _FakeOnlineSpeechDenoiser(config=None)
    online.process_outputs = [[], [0.5], []]
    online.flush_output = [0.1]
    fake_module = _make_fake_sherpa_onnx_module(online_denoiser=online)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_module)

    denoiser = StreamingDenoiser("/fake/model.onnx")
    chunks = [np.zeros(160, dtype=np.int16) for _ in range(3)]

    outputs = list(denoiser.stream(chunks))

    assert len(outputs) == 2  # the one non-empty process() output + the flush tail
    np.testing.assert_array_equal(outputs[0], [16384])
    np.testing.assert_array_equal(outputs[1], [3276])  # int16(0.1 * 32768), truncated
    assert online.flush_calls == 1


def test_streaming_denoiser_raises_runtime_error_when_config_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_module = _make_fake_sherpa_onnx_module()

    def invalid_config(**kwargs: Any) -> _FakeOnlineSpeechDenoiserConfig:
        config = _FakeOnlineSpeechDenoiserConfig(**kwargs)
        config.valid = False
        return config

    fake_module.OnlineSpeechDenoiserConfig = invalid_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_module)

    with pytest.raises(RuntimeError, match="config"):
        StreamingDenoiser("/fake/model.onnx")
