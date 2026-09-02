"""Tests for palmimo_sdk.audio.aec — EchoCanceller (AudioProcessor implementation).

``ai_edge_litert`` is assumed absent from the test environment: a fake module
(exposing just the ``interpreter.Interpreter`` slice ``DtlnResidual`` calls) is
injected via ``monkeypatch.setitem(sys.modules, ...)``, mirroring
``test_dtln.py`` / ``test_processor.py``'s ``sherpa_onnx`` fake.
``ensure_dtln_models`` is monkeypatched too, so no network access / real model
file is needed.

Construction coverage choice: rather than asserting construction *fails*
without a runtime (that's already covered by ``test_dtln.py``'s
``_load_interpreter`` test), these tests fake both the model download and the
interpreter so construction *succeeds* — this exercises the real
``DtlnResidual.__init__`` / ``GatedDtlnCanceller`` wiring inside
``EchoCanceller.__init__`` without any network or real weights.
"""

from __future__ import annotations

import sys
import types
import wave
from io import BytesIO
from typing import Any

import numpy as np
import pytest

from palmimo_sdk.audio.aec import EchoCanceller
from palmimo_sdk.audio.dtln import HOP_SAMPLES
from palmimo_sdk.audio.processor import int16_to_wav, wav_to_int16


# ----------------------------------------------------------------------
# Fake ai_edge_litert.interpreter.Interpreter — enough to satisfy
# DtlnResidual.__init__ (get_input_details/get_output_details shapes).
# ----------------------------------------------------------------------
class _FakeStageInterpreter:
    def __init__(self, model_path: str) -> None:
        self.model_path = model_path
        self.allocated = False

    def allocate_tensors(self) -> None:
        self.allocated = True

    def get_input_details(self) -> list[dict[str, Any]]:
        return [
            {"index": 0, "shape": (1, 1, 257)},
            {"index": 1, "shape": (1, 2, 128, 2)},
            {"index": 2, "shape": (1, 1, 257)},
        ]

    def get_output_details(self) -> list[dict[str, Any]]:
        return [
            {"index": 0, "shape": (1, 1, 257)},
            {"index": 1, "shape": (1, 2, 128, 2)},
        ]

    def set_tensor(self, _index: int, _value: Any) -> None:
        pass

    def invoke(self) -> None:
        pass

    def get_tensor(self, _index: int) -> Any:
        return np.zeros((1, 1, 257), dtype=np.float32)


def _install_fake_litert(monkeypatch: pytest.MonkeyPatch) -> None:
    package = types.ModuleType("ai_edge_litert")
    submodule = types.ModuleType("ai_edge_litert.interpreter")
    submodule.Interpreter = _FakeStageInterpreter  # type: ignore[attr-defined]
    package.interpreter = submodule  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ai_edge_litert", package)
    monkeypatch.setitem(sys.modules, "ai_edge_litert.interpreter", submodule)


def _patch_ensure_dtln_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "palmimo_sdk.audio.dtln.ensure_dtln_models",
        lambda size=256, model_dir=None: ("/fake/dtln_aec_256_1.tflite", "/fake/dtln_aec_256_2.tflite"),
    )


def _build(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> EchoCanceller:
    _install_fake_litert(monkeypatch)
    _patch_ensure_dtln_models(monkeypatch)
    return EchoCanceller(**kwargs)


def _multi_channel_wav(frames: int, channels: int, sample_rate: int = 16000) -> bytes:
    """Build a WAV where channel c holds ``100 * c + frame_index``."""
    rows = [[100 * channel + frame for channel in range(channels)] for frame in range(frames)]
    data = np.array(rows, dtype=np.int16)
    return int16_to_wav(data, sample_rate, channels=channels)


# ----------------------------------------------------------------------
# Construction
# ----------------------------------------------------------------------
def test_construction_succeeds_when_models_and_runtime_are_faked(monkeypatch: pytest.MonkeyPatch) -> None:
    canceller = _build(monkeypatch)
    assert canceller.sample_rate == 16000


def test_capture_channels_reflects_the_configured_channels_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    canceller = _build(monkeypatch, channels=8, near_channel=2, reference_channel=7)
    assert canceller.capture_channels == 8


def test_capture_channels_defaults_to_six(monkeypatch: pytest.MonkeyPatch) -> None:
    canceller = _build(monkeypatch)
    assert canceller.capture_channels == 6


@pytest.mark.parametrize(("near", "reference"), [(-1, 5), (6, 5), (1, 6), (1, -2)])
def test_init_rejects_a_channel_outside_the_opened_count(near: int, reference: int) -> None:
    # No model/runtime faking needed: validation happens before any lazy
    # import of the DTLN module.
    with pytest.raises(ValueError, match="outside the 6 channels"):
        EchoCanceller(near_channel=near, reference_channel=reference)


def test_init_rejects_the_same_channel_for_both_roles() -> None:
    with pytest.raises(ValueError, match="both 1"):
        EchoCanceller(near_channel=1, reference_channel=1)


# ----------------------------------------------------------------------
# process() — channel selection, via a fake GatedDtlnCanceller seam
# ----------------------------------------------------------------------
class _RecordingCanceller:
    """Stand-in for GatedDtlnCanceller: records the (near, far) pairs it is given."""

    def __init__(self, **_kwargs: Any) -> None:
        self.pairs: list[tuple[Any, Any]] = []

    def process(self, near: Any, far: Any) -> Any:
        self.pairs.append((np.array(near), np.array(far)))
        return np.asarray(near, dtype=np.int16)

    def close(self) -> None:
        pass


def test_process_selects_the_configured_near_and_reference_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_litert(monkeypatch)
    _patch_ensure_dtln_models(monkeypatch)
    recording = _RecordingCanceller()
    monkeypatch.setattr("palmimo_sdk.audio.dtln.GatedDtlnCanceller", lambda **kwargs: recording)

    canceller = EchoCanceller(channels=6, near_channel=1, reference_channel=5)
    wav_in = _multi_channel_wav(frames=8, channels=6)

    canceller.process(wav_in)

    assert len(recording.pairs) == 1
    near, far = recording.pairs[0]
    assert list(near) == [100 + frame for frame in range(8)]
    assert list(far) == [500 + frame for frame in range(8)]


def test_process_returns_mono_wav(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_litert(monkeypatch)
    _patch_ensure_dtln_models(monkeypatch)
    monkeypatch.setattr("palmimo_sdk.audio.dtln.GatedDtlnCanceller", lambda **kwargs: _RecordingCanceller())

    canceller = EchoCanceller(channels=6, near_channel=1, reference_channel=5)
    wav_in = _multi_channel_wav(frames=8, channels=6)

    wav_out = canceller.process(wav_in)

    with wave.open(BytesIO(wav_out), "rb") as reader:
        assert reader.getnchannels() == 1
    samples, sample_rate = wav_to_int16(wav_out)
    assert sample_rate == 16000
    # The fake canceller passes the near channel through untouched.
    assert list(samples) == [100 + frame for frame in range(8)]


def test_process_raises_value_error_on_sample_rate_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_litert(monkeypatch)
    _patch_ensure_dtln_models(monkeypatch)
    monkeypatch.setattr("palmimo_sdk.audio.dtln.GatedDtlnCanceller", lambda **kwargs: _RecordingCanceller())

    canceller = EchoCanceller(channels=6, near_channel=1, reference_channel=5)
    wav_in = _multi_channel_wav(frames=8, channels=6, sample_rate=8000)

    with pytest.raises(ValueError, match="16000"):
        canceller.process(wav_in)


def test_process_raises_value_error_on_channel_count_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_litert(monkeypatch)
    _patch_ensure_dtln_models(monkeypatch)
    monkeypatch.setattr("palmimo_sdk.audio.dtln.GatedDtlnCanceller", lambda **kwargs: _RecordingCanceller())

    canceller = EchoCanceller(channels=6, near_channel=1, reference_channel=5)
    wrong_channel_count_wav = _multi_channel_wav(frames=8, channels=4)

    with pytest.raises(ValueError, match="6 channels"):
        canceller.process(wrong_channel_count_wav)


# ----------------------------------------------------------------------
# Partial-hop buffering across the WAV en/decode boundary — the underlying
# GatedDtlnCanceller carry logic itself is covered in test_dtln.py; this
# proves EchoCanceller.process()'s WAV round trip doesn't break it.
# ----------------------------------------------------------------------
class _FakeResidual:
    hop = HOP_SAMPLES

    def process(self, near: Any, _far: Any) -> Any:
        return np.zeros(len(near), dtype=np.float32)

    def close(self) -> None:
        pass


def test_partial_hop_buffering_survives_the_wav_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """What goes in eventually comes out, even split across two process() calls."""
    _install_fake_litert(monkeypatch)
    _patch_ensure_dtln_models(monkeypatch)
    monkeypatch.setattr("palmimo_sdk.audio.dtln.DtlnResidual.load", classmethod(lambda cls, *a, **kw: _FakeResidual()))

    canceller = EchoCanceller(channels=6, near_channel=1, reference_channel=5)
    partial_frames = HOP_SAMPLES - 20  # less than one hop
    first_wav = _multi_channel_wav(frames=partial_frames, channels=6)
    second_wav = _multi_channel_wav(frames=partial_frames, channels=6)

    first_out, _sr1 = wav_to_int16(canceller.process(first_wav))
    second_out, _sr2 = wav_to_int16(canceller.process(second_wav))

    # Nothing whole to emit from the first (partial) hop; the combined two
    # partial hops fill (and emit) exactly one whole hop from the second call.
    assert len(first_out) == 0
    assert len(second_out) == HOP_SAMPLES
