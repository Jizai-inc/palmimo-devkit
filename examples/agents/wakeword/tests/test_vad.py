"""Tests for :mod:`palmimo_wakeword_agent.vad` (no hardware, no ONNX, no network).

:class:`UtteranceSegmenter` is exercised against a scripted fake VAD that
returns queued probabilities and records every frame it was given, so these
tests assert both the state machine behavior and the internal 512-sample
rebuffering contract without loading a real Silero VAD model.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest

from palmimo_wakeword_agent.vad import (
    Int16Array,
    UtteranceSegmenter,
    _new_inference_session,
    session_options,
)


FRAME = 512


class FakeVad:
    """Scripted VAD: pops queued probabilities per call, records frame lengths."""

    def __init__(self, probs: list[float]) -> None:
        self._probs = list(probs)
        self.frame_lengths: list[int] = []
        self.reset_calls = 0

    def __call__(self, frame: Int16Array) -> float:
        self.frame_lengths.append(len(frame))
        if not self._probs:
            return 0.0
        return self._probs.pop(0)

    def reset(self) -> None:
        self.reset_calls += 1


def _chunk(n: int, value: int = 100) -> Int16Array:
    return np.full(n, value, dtype=np.int16)


def test_segmenter_frame_lengths_are_always_512() -> None:
    vad = FakeVad([0.0] * 20)
    seg = UtteranceSegmenter(vad=vad, silence_seconds=0.1)
    for _ in range(20):
        seg.feed(_chunk(FRAME))
    assert vad.frame_lengths
    assert all(length == FRAME for length in vad.frame_lengths)


def test_segmenter_rebuffers_variable_length_chunks() -> None:
    vad = FakeVad([0.0] * 20)
    seg = UtteranceSegmenter(vad=vad, silence_seconds=0.1)
    # Feed odd-sized chunks that don't align to 512-sample boundaries.
    for size in (300, 400, 250, 90, 512, 1000, 37):
        seg.feed(_chunk(size))
    assert all(length == FRAME for length in vad.frame_lengths)


def test_segmenter_starts_on_threshold_crossing_with_pre_roll() -> None:
    # Two idle frames (pre-roll), then a crossing frame, then silence to finish.
    silence_frames = int(0.8 * 16000 / FRAME) + 2
    probs = [0.1, 0.1, 0.9] + [0.0] * silence_frames
    vad = FakeVad(probs)
    seg = UtteranceSegmenter(vad=vad, start_threshold=0.5, end_threshold=0.35, silence_seconds=0.8)

    result = None
    n_frames = len(probs)
    for _ in range(n_frames):
        result = seg.feed(_chunk(FRAME))
        if result is not None:
            break

    assert result is not None
    # Pre-roll (2 frames) + crossing frame + silence frames until completion.
    assert len(result) >= FRAME * 3


def test_segmenter_ignores_sub_threshold_noise() -> None:
    vad = FakeVad([0.1] * 50)
    seg = UtteranceSegmenter(vad=vad, start_threshold=0.5)
    result = None
    for _ in range(50):
        result = seg.feed(_chunk(FRAME))
    assert result is None


def test_segmenter_completes_after_sustained_silence() -> None:
    silence_frames = int(0.8 * 16000 / FRAME) + 1
    probs = [0.9] + [0.0] * silence_frames
    vad = FakeVad(probs)
    seg = UtteranceSegmenter(vad=vad, start_threshold=0.5, end_threshold=0.35, silence_seconds=0.8)

    result = None
    for _ in range(len(probs)):
        result = seg.feed(_chunk(FRAME))
        if result is not None:
            break
    assert result is not None


def test_segmenter_end_threshold_resets_silence_run() -> None:
    # Speech, a brief dip below end_threshold that is NOT sustained, more
    # speech, then real sustained silence — the brief dip must not finalize.
    silence_frames = int(0.8 * 16000 / FRAME) + 1
    probs = [0.9, 0.9, 0.1, 0.1, 0.9, 0.9] + [0.0] * silence_frames
    vad = FakeVad(probs)
    seg = UtteranceSegmenter(vad=vad, start_threshold=0.5, end_threshold=0.35, silence_seconds=0.8)

    completions = 0
    total = 0
    for _ in range(len(probs)):
        result = seg.feed(_chunk(FRAME))
        total += 1
        if result is not None:
            completions += 1
            break
    assert completions == 1
    # It should not have finalized right after the brief dip (frame index 4).
    assert total > 4


def test_segmenter_completes_at_max_duration() -> None:
    max_seconds = 1.0
    n_frames_for_max = int(max_seconds * 16000 / FRAME) + 5
    probs = [0.9] * n_frames_for_max
    vad = FakeVad(probs)
    seg = UtteranceSegmenter(vad=vad, start_threshold=0.5, max_utterance_seconds=max_seconds, silence_seconds=100.0)

    result = None
    for _ in range(n_frames_for_max):
        result = seg.feed(_chunk(FRAME))
        if result is not None:
            break
    assert result is not None
    assert len(result) >= int(max_seconds * 16000)


def test_segmenter_carries_remainder_across_feeds() -> None:
    vad = FakeVad([0.0] * 20)
    seg = UtteranceSegmenter(vad=vad, silence_seconds=0.1)
    seg.feed(_chunk(100))
    assert vad.frame_lengths == []  # not enough samples yet for one frame
    seg.feed(_chunk(500))
    assert vad.frame_lengths == [FRAME]  # 100 + 500 = 600 -> one 512 frame, 88 left over


def test_segmenter_reset_drops_buffered_state() -> None:
    vad = FakeVad([0.9] * 5)
    seg = UtteranceSegmenter(vad=vad, start_threshold=0.5, silence_seconds=100.0)
    seg.feed(_chunk(FRAME))
    assert seg.feed(_chunk(300)) is None  # mid-utterance, buffered remainder pending
    seg.reset()
    # After reset, previously buffered remainder must not leak into new frames.
    vad2 = FakeVad([0.0] * 10)
    seg._vad = vad2
    seg.feed(_chunk(212))  # would complete the old 512 boundary if remainder leaked
    assert vad2.frame_lengths == []


def test_segmenter_reset_also_resets_the_vad_recurrent_state() -> None:
    vad = FakeVad([0.0])
    seg = UtteranceSegmenter(vad=vad)
    seg.reset()
    assert vad.reset_calls == 1


def test_segmenter_calls_vad_reset_on_completion() -> None:
    silence_frames = int(0.8 * 16000 / FRAME) + 1
    probs = [0.9] + [0.0] * silence_frames
    vad = FakeVad(probs)
    seg = UtteranceSegmenter(vad=vad, start_threshold=0.5, end_threshold=0.35, silence_seconds=0.8)
    for _ in range(len(probs)):
        if seg.feed(_chunk(FRAME)) is not None:
            break
    assert vad.reset_calls == 1


def test_segmenter_no_reset_method_is_tolerated() -> None:
    class VadWithoutReset:
        def __call__(self, frame: Int16Array) -> float:
            return 0.9 if len(vad_calls) < 1 else 0.0

    vad_calls: list[int] = []
    vad = VadWithoutReset()
    silence_frames = int(0.8 * 16000 / FRAME) + 1
    seg = UtteranceSegmenter(vad=vad, start_threshold=0.5, end_threshold=0.35, silence_seconds=0.8)
    for i in range(silence_frames + 2):
        vad_calls.append(i)
        result = seg.feed(_chunk(FRAME))
        if result is not None:
            break
    # Must not raise even though `vad` has no reset().


def test_vad_load_downloads_and_verifies_sha256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from palmimo_wakeword_agent import vad as vad_module

    calls = []

    def fake_download(url: str, dest: Path, *, timeout: float = 30.0, sha256: str | None = None) -> None:
        calls.append((url, str(dest), sha256))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-model-bytes")

    fake_session = object()

    class FakeSessionFactory:
        def __init__(self) -> None:
            self.created_with: list[str] = []

        def __call__(self, path: str) -> object:
            self.created_with.append(path)
            return fake_session

    factory = FakeSessionFactory()
    monkeypatch.setattr(vad_module, "download_atomic", fake_download)
    monkeypatch.setattr(vad_module, "_new_inference_session", factory)

    model_path = tmp_path / "models" / "silero_vad.onnx"
    result = vad_module.SileroVad.load(model_path)

    assert calls[0][0] == vad_module.SILERO_VAD_MODEL_URL
    assert calls[0][2] == vad_module.SILERO_VAD_MODEL_SHA256
    assert factory.created_with == [str(model_path)]
    assert result._session is fake_session


def test_segmenter_keeps_unscored_tail_as_carry_when_utterance_completes_mid_chunk() -> None:
    # One chunk holds: the crossing frame, enough silence to complete the
    # utterance, and one extra frame beyond the boundary. That extra frame
    # must NOT be scored with this feed() call (the utterance already
    # completed) and must NOT be lost — it is carried into the next feed().
    silence_frames = int(0.1 * 16000 / FRAME) + 1
    vad = FakeVad([0.9] + [0.0] * silence_frames + [0.9, 0.9])
    seg = UtteranceSegmenter(vad=vad, silence_seconds=0.1)

    frames_to_complete = 1 + silence_frames
    result = seg.feed(_chunk(FRAME * (frames_to_complete + 1)))
    assert result is not None
    assert len(vad.frame_lengths) == frames_to_complete  # tail frame not scored yet

    # The carried tail frame is scored on the next feed: one small top-up
    # chunk should produce exactly one more VAD call (512 carried + 512 new).
    seg.feed(_chunk(FRAME))
    assert len(vad.frame_lengths) == frames_to_complete + 2


# --- the ONNX Runtime session policy ---


class _FakeSessionOptions:
    """Records what `session_options` sets, standing in for `ort.SessionOptions`."""

    def __init__(self) -> None:
        self.intra_op_num_threads: int | None = None
        self.inter_op_num_threads: int | None = None
        self.execution_mode: object = None
        self.config_entries: dict[str, str] = {}

    def add_session_config_entry(self, key: str, value: str) -> None:
        self.config_entries[key] = value


class _FakeExecutionMode:
    ORT_SEQUENTIAL = "sequential"
    ORT_PARALLEL = "parallel"


class _FakeOrt:
    """The slice of the `onnxruntime` module `session_options` touches."""

    SessionOptions = _FakeSessionOptions
    ExecutionMode = _FakeExecutionMode


class _FakeInferenceSession:
    def __init__(self, path: str, sess_options: Any = None) -> None:
        self.path = path
        self.sess_options = sess_options


class _FakeOrtModule(_FakeOrt):
    InferenceSession = _FakeInferenceSession


def test_new_inference_session_applies_the_session_options(monkeypatch: pytest.MonkeyPatch) -> None:
    # The options only matter if they reach the session; asserting the builder
    # alone would stay green if `sess_options=` were ever dropped here.
    monkeypatch.setitem(sys.modules, "onnxruntime", _FakeOrtModule)

    session = cast("_FakeInferenceSession", _new_inference_session("silero_vad.onnx"))

    assert session.sess_options is not None
    assert session.sess_options.intra_op_num_threads == 1
    assert session.sess_options.config_entries["session.intra_op.allow_spinning"] == "0"


def test_session_options_pin_one_non_spinning_thread() -> None:
    # Left at the defaults, ONNX Runtime spins an intra-op thread per core
    # between inferences that arrive 32 ms apart -- measured at 242% of one
    # core on a Raspberry Pi 5, against 2.5% with these options.
    options = session_options(_FakeOrt)

    assert options.intra_op_num_threads == 1
    assert options.inter_op_num_threads == 1
    assert options.execution_mode == _FakeExecutionMode.ORT_SEQUENTIAL
    assert options.config_entries["session.intra_op.allow_spinning"] == "0"
