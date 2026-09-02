"""Tests for palmimo_sdk.audio.dtln — GatedDtlnCanceller gating policy and
_load_interpreter's runtime-selection seam.

The gating tests are driven through a fake residual, mirroring the
``origin/feat/aec`` reference test suite, so the policy is exercised without
downloading or running the real DTLN-AEC model. A fake ``ai_edge_litert``
module is injected into ``sys.modules`` for the ``_load_interpreter`` test, so
no real LiteRT runtime is needed either.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import numpy as np
import pytest

from palmimo_sdk.audio.dtln import HOP_SAMPLES, GatedDtlnCanceller, _load_interpreter


class _FakeResidual:
    """Returns a constant, and counts how often it was actually run."""

    hop = HOP_SAMPLES

    def __init__(self, value: float = 0.0) -> None:
        self.value = value
        self.calls = 0
        self.closed = False

    def process(self, near: Any, _far: Any) -> Any:
        self.calls += 1
        return np.full(len(near), self.value, dtype=np.float32)

    def close(self) -> None:
        self.closed = True


def _loud(hops: int = 1, value: int = 8000) -> Any:
    return np.full(hops * HOP_SAMPLES, value, dtype=np.int16)


def _silent(hops: int = 1) -> Any:
    return np.zeros(hops * HOP_SAMPLES, dtype=np.int16)


def _canceller(**kwargs: Any) -> tuple[GatedDtlnCanceller, _FakeResidual]:
    residual = _FakeResidual(kwargs.pop("value", 0.0))
    return GatedDtlnCanceller(residual=residual, **kwargs), residual


# ----------------------------------------------------------------------
# GatedDtlnCanceller — gating behavior
# ----------------------------------------------------------------------


def test_it_cancels_while_the_loudspeaker_is_playing() -> None:
    canceller, _residual = _canceller()

    out = canceller.process(_loud(), _loud())

    # The fake returns silence, so a cancelled hop is silent.
    assert out.shape == (HOP_SAMPLES,)
    assert int(np.abs(out).max()) == 0


def test_the_microphone_passes_through_while_the_loudspeaker_is_silent() -> None:
    canceller, _residual = _canceller()

    out = canceller.process(_loud(), _silent())

    assert list(out) == [8000] * HOP_SAMPLES


def test_no_inference_runs_while_the_loudspeaker_is_silent() -> None:
    """Its output would be discarded, and the cost overflows the capture device."""
    canceller, residual = _canceller()

    canceller.process(_loud(4), _silent(4))

    assert residual.calls == 0


def test_the_hangover_keeps_it_engaged_after_playback_stops() -> None:
    canceller, _residual = _canceller(hangover_hops=2)

    canceller.process(_loud(), _loud())
    out = canceller.process(_loud(3), _silent(3))

    # Two hops still inside the hangover are cancelled; the third is not.
    assert int(np.abs(out[: 2 * HOP_SAMPLES]).max()) == 0
    assert list(out[2 * HOP_SAMPLES :]) == [8000] * HOP_SAMPLES


def test_the_hangover_is_measured_in_hops_not_calls() -> None:
    canceller, _residual = _canceller(hangover_hops=2)

    canceller.process(_loud(), _loud())
    out = canceller.process(_loud(4), _silent(4))

    assert int(np.abs(out[:HOP_SAMPLES]).max()) == 0
    assert int(np.abs(out[3 * HOP_SAMPLES :]).max()) == 8000


def test_a_partial_hop_is_carried_into_the_next_call() -> None:
    canceller, _residual = _canceller()

    first = canceller.process(_loud()[:100], _loud()[:100])
    second = canceller.process(_loud()[:100], _loud()[:100])

    assert len(first) == 0  # nothing whole to emit yet
    assert len(second) == HOP_SAMPLES


def test_the_streams_are_consumed_in_lockstep() -> None:
    canceller, residual = _canceller()

    canceller.process(_loud(3), _loud(1))

    assert residual.calls == 1


def test_it_counts_how_much_of_the_session_was_filtered() -> None:
    canceller, _residual = _canceller(hangover_hops=1)

    canceller.process(_loud(2), _loud(2))
    canceller.process(_loud(2), _silent(2))

    assert canceller.hops_processed == 4
    assert canceller.hops_engaged == 3  # two playing, one of hangover


def test_close_releases_the_model_and_is_permanent() -> None:
    canceller, residual = _canceller()

    canceller.close()

    assert residual.closed is True
    with pytest.raises(RuntimeError, match="closed"):
        canceller.process(_loud(), _loud())


# ----------------------------------------------------------------------
# _load_interpreter — LiteRT runtime seam
# ----------------------------------------------------------------------


class _FakeInterpreter:
    """Stand-in for ai_edge_litert.interpreter.Interpreter."""

    def __init__(self, model_path: str) -> None:
        self.model_path = model_path
        self.allocated = False

    def allocate_tensors(self) -> None:
        self.allocated = True


def _make_fake_ai_edge_litert_module(interpreter_cls: type = _FakeInterpreter) -> types.ModuleType:
    package = types.ModuleType("ai_edge_litert")
    submodule = types.ModuleType("ai_edge_litert.interpreter")
    submodule.Interpreter = interpreter_cls  # type: ignore[attr-defined]
    package.interpreter = submodule  # type: ignore[attr-defined]
    return package


def test_load_interpreter_builds_and_allocates_via_ai_edge_litert(monkeypatch: pytest.MonkeyPatch) -> None:
    package = _make_fake_ai_edge_litert_module()
    monkeypatch.setitem(sys.modules, "ai_edge_litert", package)
    monkeypatch.setitem(sys.modules, "ai_edge_litert.interpreter", package.interpreter)

    interpreter = _load_interpreter("/fake/model.tflite")

    assert isinstance(interpreter, _FakeInterpreter)
    assert interpreter.model_path == "/fake/model.tflite"
    assert interpreter.allocated is True


def test_load_interpreter_raises_runtime_error_when_no_runtime_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Setting a sys.modules entry to None makes the matching import statement
    # raise ImportError (a documented import-blocking trick), so all three
    # runtime fallbacks _load_interpreter tries are forced to fail regardless
    # of what's actually installed in this environment (ai-edge-litert itself
    # is a declared `voice`-extra dependency now, so it IS importable here).
    monkeypatch.setitem(sys.modules, "ai_edge_litert.interpreter", None)
    monkeypatch.setitem(sys.modules, "tflite_runtime.interpreter", None)
    monkeypatch.setitem(sys.modules, "tensorflow", None)

    with pytest.raises(RuntimeError, match="uv add ai-edge-litert"):
        _load_interpreter("/fake/model.tflite")
