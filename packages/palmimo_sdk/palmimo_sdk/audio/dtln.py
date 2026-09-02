# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""DTLN-AEC acoustic echo cancellation.

Removes the loudspeaker from the microphone signal, given the far-end
reference. Two models run in series per frame: the first masks the magnitude
spectrum, the second refines in the time domain. Both carry recurrent state,
so an instance belongs to one stream.

Inference runs on LiteRT (``ai-edge-litert``). Weights are downloaded on first
use and pinned by sha256.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from ..download import default_model_dir, download_atomic


#: Analysis window, in samples. Fixed by the model architecture.
BLOCK_SAMPLES = 512

#: Samples consumed and produced per :meth:`DtlnResidual.process` call.
HOP_SAMPLES = 128

#: Model sizes published upstream, by frame width.
SUPPORTED_SIZES = (128, 256, 512)

DEFAULT_SIZE = 256

#: Reference level, in dBFS, below which the loudspeaker counts as silent.
FAR_END_SILENCE_DB = -60.0

#: Hops the canceller stays engaged after the reference goes quiet, covering
#: the echo tail. 8 hops is 64 ms; the measured echo path is under 8 ms.
HANGOVER_HOPS = 8

_MODEL_BASE_URL = "https://raw.githubusercontent.com/breizhn/DTLN-aec/master/pretrained_models/"

MODEL_SHA256: dict[str, str] = {
    "dtln_aec_128_1.tflite": "8d241b3a732af8ca140b2e30043e56a6c3c7800c46e22c26f4eea2f70974ad1e",
    "dtln_aec_128_2.tflite": "350bb01a1152ae3cabe09fe5e868ef2f7d8b988a9f22aae44f140195f6493126",
    "dtln_aec_256_1.tflite": "4a3a588b69fd79d837bc068b579a26faa92cac39dddbb00001d2dc1c3d869d60",
    "dtln_aec_256_2.tflite": "fa2590243aad1bf893c5be45b20709e8c50feec65e3604d1d52bae6eeddc23d3",
    "dtln_aec_512_1.tflite": "569f7c3cfac96b1e093229c3ca10b5d892f5b1906105b644e457f8245b4f7383",
    "dtln_aec_512_2.tflite": "fb423d867ab25d5f4716bd369c7126b6c84926175c019b832e71bf21de0e9907",
}

_RUNTIME_INSTALL_HINT = "uv add ai-edge-litert"

_log = logging.getLogger(__name__)


def model_filenames(size: int = DEFAULT_SIZE) -> tuple[str, str]:
    """Return the stage-one and stage-two filenames for *size*.

    Args:
        size: One of :data:`SUPPORTED_SIZES`.

    Returns:
        The two model filenames.

    Raises:
        ValueError: If *size* is not a published model size.
    """
    if size not in SUPPORTED_SIZES:
        raise ValueError(f"size must be one of {SUPPORTED_SIZES}, got {size}")
    return f"dtln_aec_{size}_1.tflite", f"dtln_aec_{size}_2.tflite"


def ensure_dtln_models(size: int = DEFAULT_SIZE, model_dir: str | None = None) -> tuple[str, str]:
    """Resolve both model files locally, downloading them if missing.

    Args:
        size: One of :data:`SUPPORTED_SIZES`.
        model_dir: Cache directory; None uses the shared model cache.

    Returns:
        Absolute paths to the stage-one and stage-two models.

    Raises:
        RuntimeError: If a model is absent and cannot be downloaded.
        ValueError: If *size* is not a published model size.
    """
    directory = Path(os.path.expanduser(model_dir)) if model_dir is not None else default_model_dir()
    resolved: list[str] = []
    for name in model_filenames(size):
        path = directory / name
        if not path.exists():
            _log.info("downloading %s (first use)", name)
            try:
                download_atomic(_MODEL_BASE_URL + name, path, timeout=120.0, sha256=MODEL_SHA256[name])
            except OSError as exc:
                raise RuntimeError(
                    f"could not download the DTLN-AEC model {name} to {path}: {exc}. "
                    f"Fetch it manually from {_MODEL_BASE_URL + name} and place it there."
                ) from exc
        resolved.append(str(path))
    return resolved[0], resolved[1]


def _load_interpreter(model_path: str) -> Any:
    """Build an interpreter for *model_path* on whichever runtime is installed.

    Args:
        model_path: Path to a ``.tflite`` model.

    Returns:
        An interpreter with tensors already allocated.

    Raises:
        RuntimeError: If no supported runtime is installed.
    """
    for module_name, attribute in (
        ("ai_edge_litert.interpreter", "Interpreter"),
        ("tflite_runtime.interpreter", "Interpreter"),
    ):
        try:
            module = __import__(module_name, fromlist=[attribute])
        except ImportError:
            continue
        interpreter = getattr(module, attribute)(model_path=model_path)
        interpreter.allocate_tensors()
        return interpreter
    try:
        import tensorflow as tf
    except ImportError:
        raise RuntimeError(f"no LiteRT runtime is installed. Install one with `{_RUNTIME_INSTALL_HINT}`.") from None
    interpreter = tf.lite.Interpreter(model_path=model_path)
    interpreter.allocate_tensors()
    return interpreter


def _level_db(samples: Any) -> float:
    """RMS level of *samples* in dBFS, floored so digital silence stays finite."""
    import numpy as np

    if samples.size == 0:
        return -180.0
    return float(20.0 * np.log10(float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) + 1e-9))


class DtlnResidual:
    """The DTLN-AEC model pair.

    Stateful and single-threaded. Consumes and returns exactly
    :data:`HOP_SAMPLES` float32 samples in [-1, 1] per call.

    Attributes:
        sample_rate: Rate the models are trained for.
        hop: Samples consumed and produced per call.
    """

    def __init__(self, stage_one_path: str, stage_two_path: str, *, sample_rate: int = 16000) -> None:
        """Load both model stages and allocate their buffers.

        Args:
            stage_one_path: Path to the magnitude-masking model.
            stage_two_path: Path to the time-domain refinement model.
            sample_rate: Rate of the audio to be processed, in Hz.

        Raises:
            RuntimeError: If no LiteRT runtime is installed.
        """
        import numpy as np

        self.sample_rate = sample_rate
        self.hop = HOP_SAMPLES
        self._stage_one = _load_interpreter(stage_one_path)
        self._stage_two = _load_interpreter(stage_two_path)
        self._in_one = self._stage_one.get_input_details()
        self._out_one = self._stage_one.get_output_details()
        self._in_two = self._stage_two.get_input_details()
        self._out_two = self._stage_two.get_output_details()
        self._state_one = np.zeros(self._in_one[1]["shape"], dtype=np.float32)
        self._state_two = np.zeros(self._in_two[1]["shape"], dtype=np.float32)
        # Sliding analysis windows: each call shifts in one hop, so the models
        # always see a whole BLOCK_SAMPLES of context.
        self._near_window = np.zeros(BLOCK_SAMPLES, dtype=np.float32)
        self._far_window = np.zeros(BLOCK_SAMPLES, dtype=np.float32)
        self._overlap = np.zeros(BLOCK_SAMPLES, dtype=np.float32)

    @classmethod
    def load(cls, size: int = DEFAULT_SIZE, model_dir: str | None = None, *, sample_rate: int = 16000) -> DtlnResidual:
        """Build an instance, downloading the models on first use.

        Args:
            size: One of :data:`SUPPORTED_SIZES`.
            model_dir: Cache directory; None uses the shared model cache.
            sample_rate: Rate of the audio to be processed, in Hz.

        Returns:
            A ready instance.
        """
        stage_one, stage_two = ensure_dtln_models(size, model_dir)
        return cls(stage_one, stage_two, sample_rate=sample_rate)

    def process(self, near: Any, far: Any) -> Any:
        """Suppress the echo in *near*, given the reference *far*.

        Args:
            near: :data:`HOP_SAMPLES` float32 microphone samples.
            far: :data:`HOP_SAMPLES` float32 far-end reference samples.

        Returns:
            :data:`HOP_SAMPLES` float32 samples.
        """
        import numpy as np

        hop = self.hop
        self._near_window = np.roll(self._near_window, -hop)
        self._near_window[-hop:] = near
        self._far_window = np.roll(self._far_window, -hop)
        self._far_window[-hop:] = far

        spectrum = np.fft.rfft(self._near_window).astype(np.complex64)
        self._stage_one.set_tensor(self._in_one[0]["index"], np.abs(spectrum).reshape(1, 1, -1).astype(np.float32))
        self._stage_one.set_tensor(
            self._in_one[2]["index"], np.abs(np.fft.rfft(self._far_window)).reshape(1, 1, -1).astype(np.float32)
        )
        self._stage_one.set_tensor(self._in_one[1]["index"], self._state_one)
        self._stage_one.invoke()
        self._state_one = self._stage_one.get_tensor(self._out_one[1]["index"])
        mask = self._stage_one.get_tensor(self._out_one[0]["index"]).squeeze()

        estimate = np.fft.irfft(spectrum * mask).astype(np.float32)
        self._stage_two.set_tensor(self._in_two[0]["index"], estimate.reshape(1, 1, -1))
        self._stage_two.set_tensor(self._in_two[2]["index"], self._far_window.reshape(1, 1, -1).astype(np.float32))
        self._stage_two.set_tensor(self._in_two[1]["index"], self._state_two)
        self._stage_two.invoke()
        self._state_two = self._stage_two.get_tensor(self._out_two[1]["index"])
        refined = self._stage_two.get_tensor(self._out_two[0]["index"]).squeeze()

        # Overlap-add: only the oldest hop of the accumulator is complete.
        self._overlap = np.roll(self._overlap, -hop)
        self._overlap[-hop:] = 0.0
        self._overlap = self._overlap + refined
        return self._overlap[:hop].copy()

    def close(self) -> None:
        """Release both interpreters. Idempotent."""
        self._stage_one = None
        self._stage_two = None


class GatedDtlnCanceller:
    """DTLN-AEC over an int16 stream, applied only while the loudspeaker is on.

    DTLN is a suppressor and cannot tell whose voice it is looking at, so
    running it continuously attenuates the near-end speaker as well. While the
    reference is silent the microphone passes through untouched and no
    inference runs.

    Stateful and single-threaded. Samples that do not fill a whole hop are
    carried into the next call, so the output may be one hop shorter than the
    input.

    Attributes:
        hops_processed: Hops seen, in total.
        hops_engaged: Hops the loudspeaker was judged active for.
    """

    def __init__(
        self,
        *,
        residual: Any = None,
        size: int = DEFAULT_SIZE,
        far_end_silence_db: float = FAR_END_SILENCE_DB,
        hangover_hops: int = HANGOVER_HOPS,
    ) -> None:
        """Build the canceller, loading the model if one was not supplied.

        Args:
            residual: A loaded :class:`DtlnResidual`, or None to load one.
            size: Model width to load when *residual* is None.
            far_end_silence_db: Reference level below which the loudspeaker
                counts as silent.
            hangover_hops: Hops to stay engaged after the reference falls quiet.
        """
        self.residual = residual if residual is not None else DtlnResidual.load(size)
        self.far_end_silence_db = far_end_silence_db
        self.hangover_hops = hangover_hops
        self.hop = int(getattr(self.residual, "hop", HOP_SAMPLES))
        self.hops_processed = 0
        self.hops_engaged = 0
        self._hangover = 0
        self._near_carry: Any = None
        self._far_carry: Any = None
        self._closed = False

    def _engaged(self, reference: Any) -> bool:
        """Whether this hop has echo in it worth removing."""
        self.hops_processed += 1
        if _level_db(reference) > self.far_end_silence_db:
            self._hangover = self.hangover_hops
            engaged = True
        else:
            # Read the counter, then spend it: decrementing first would make
            # hangover_hops=1 mean no hangover at all.
            engaged = self._hangover > 0
            if engaged:
                self._hangover -= 1
        if engaged:
            self.hops_engaged += 1
        return engaged

    def process(self, near: Any, far: Any) -> Any:
        """Remove *far* from *near*.

        Args:
            near: 1-D int16 microphone samples.
            far: 1-D int16 far-end reference, sample-aligned with *near*.

        Returns:
            1-D int16 samples, a whole number of hops long.

        Raises:
            RuntimeError: If the canceller has been closed.
        """
        import numpy as np

        if self._closed:
            raise RuntimeError("GatedDtlnCanceller is closed")
        near_arr = np.ascontiguousarray(near, dtype=np.int16)
        far_arr = np.ascontiguousarray(far, dtype=np.int16)
        if self._near_carry is not None and len(self._near_carry):
            near_arr = np.concatenate([self._near_carry, near_arr])
        if self._far_carry is not None and len(self._far_carry):
            far_arr = np.concatenate([self._far_carry, far_arr])

        # Consumed in lockstep: a surplus on either side is held back rather
        # than paired with samples it was not aligned to.
        usable = min(len(near_arr), len(far_arr))
        count = usable // self.hop
        self._near_carry = near_arr[count * self.hop :].copy()
        self._far_carry = far_arr[count * self.hop :].copy()
        if count == 0:
            return np.empty(0, dtype=np.int16)

        out = np.empty(count * self.hop, dtype=np.int16)
        for index in range(count):
            start = index * self.hop
            stop = start + self.hop
            reference = far_arr[start:stop].astype(np.float32) / 32768.0
            if not self._engaged(reference):
                out[start:stop] = near_arr[start:stop]
                continue
            microphone = near_arr[start:stop].astype(np.float32) / 32768.0
            cleaned = np.asarray(self.residual.process(microphone, reference), dtype=np.float32)
            out[start:stop] = np.clip(cleaned * 32768.0, -32768, 32767).astype(np.int16)
        return out

    def close(self) -> None:
        """Release the model. Idempotent."""
        self._closed = True
        residual = self.residual
        self.residual = None
        if residual is not None and hasattr(residual, "close"):
            residual.close()


__all__ = [
    "BLOCK_SAMPLES",
    "DEFAULT_SIZE",
    "FAR_END_SILENCE_DB",
    "HANGOVER_HOPS",
    "HOP_SAMPLES",
    "MODEL_SHA256",
    "SUPPORTED_SIZES",
    "DtlnResidual",
    "GatedDtlnCanceller",
    "ensure_dtln_models",
    "model_filenames",
]
