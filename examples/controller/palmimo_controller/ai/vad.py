"""Silero VAD v5 and the utterance segmenter that turns mic chunks into whole utterances.

Mirrors `palmimo_companion_agent.pipeline.vad` (same model, same state machine)
rather than importing it: each example is a standalone project, so the two are
never cross-wired.
"""

from __future__ import annotations

import collections
import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

import numpy as np
import numpy.typing as npt

from palmimo_sdk.download import download_atomic


_LOG = logging.getLogger(__name__)

#: A mono int16 PCM buffer: one frame, one chunk, or one whole utterance.
type Int16Array = npt.NDArray[np.int16]

#: Every rate in this pipeline: the VAD window, and the WAV sent to STT.
SAMPLE_RATE = 16000

#: Samples per Silero VAD v5 frame (32 ms at 16 kHz).
FRAME_SAMPLES = 512

SILERO_VAD_MODEL_URL = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/src/silero_vad/data/silero_vad.onnx"
)
SILERO_VAD_MODEL_SHA256 = "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f"

_MODEL_FILENAME = "silero_vad.onnx"


class OnnxSessionLike(Protocol):
    """The part of `onnxruntime.InferenceSession` the VAD calls."""

    def run(
        self,
        output_names: Sequence[str] | None,
        input_feed: Mapping[str, npt.NDArray[np.floating] | npt.NDArray[np.int64]],
    ) -> Sequence[npt.NDArray[np.float32]]: ...


def session_options(ort: Any) -> Any:
    """Single-threaded, non-spinning options for a graph run once every 32 ms.

    ONNX Runtime's defaults spin a core-sized thread pool between inferences:
    on a Raspberry Pi 5 that measured 242% of one core, against 2.5% with these.
    """
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    return options


def _new_inference_session(path: str) -> OnnxSessionLike:
    import onnxruntime as ort  # lazy: tests never load the real model

    return cast("OnnxSessionLike", ort.InferenceSession(path, sess_options=session_options(ort)))


class SileroVad:
    """Speech probability for one `FRAME_SAMPLES` int16 frame, with recurrent state."""

    def __init__(self, session: OnnxSessionLike) -> None:
        self._session = session
        self._state: npt.NDArray[np.float32] = np.zeros((2, 1, 128), dtype=np.float32)

    @classmethod
    def load(cls, model_path: Path | None = None) -> SileroVad:
        """Build the VAD, downloading the model into the shared Palmimo cache on first use."""
        if model_path is None:
            from palmimo_sdk.audio import default_model_dir

            model_path = default_model_dir() / _MODEL_FILENAME
        if not model_path.exists():
            download_atomic(SILERO_VAD_MODEL_URL, model_path, timeout=60.0, sha256=SILERO_VAD_MODEL_SHA256)
        return cls(_new_inference_session(str(model_path)))

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def __call__(self, frame: Int16Array) -> float:
        inputs: dict[str, npt.NDArray[np.floating] | npt.NDArray[np.int64]] = {
            "input": (frame.astype(np.float32) / 32768.0)[None, :],
            "state": self._state,
            "sr": np.array(SAMPLE_RATE, dtype=np.int64),
        }
        output, self._state = self._session.run(None, inputs)
        return float(output[0][0])


class UtteranceSegmenter:
    """Cuts a stream of 16 kHz int16 chunks of any length into whole utterances.

    Idle frames below `start_threshold` are kept as pre-roll so an utterance's
    first syllable is not clipped. While speaking, a probability at or above
    `end_threshold` resets the silence run; `silence_seconds` of silence, or
    `max_utterance_seconds` of speech, completes the utterance.

    Args:
        vad: One `FRAME_SAMPLES` frame in, a speech probability out. Its
            `reset()`, if it has one, runs whenever an utterance ends.
    """

    def __init__(
        self,
        vad: Callable[[Int16Array], float],
        *,
        start_threshold: float = 0.5,
        end_threshold: float = 0.35,
        silence_seconds: float = 0.7,
        max_utterance_seconds: float = 15.0,
        pre_roll_seconds: float = 0.3,
    ) -> None:
        self._vad = vad
        self._start_threshold = start_threshold
        self._end_threshold = end_threshold
        self._silence_limit = int(silence_seconds * SAMPLE_RATE)
        self._max_samples = int(max_utterance_seconds * SAMPLE_RATE)
        self._pre_roll_limit = int(pre_roll_seconds * SAMPLE_RATE)
        self._clear()

    def _clear(self) -> None:
        self._carry: Int16Array = np.empty(0, dtype=np.int16)
        self._pre_roll: collections.deque[Int16Array] = collections.deque()
        self._pre_roll_total = 0
        self._speech: list[Int16Array] = []
        self._speech_total = 0
        self._speaking = False
        self._silence_run = 0

    @property
    def speaking(self) -> bool:
        """Whether an utterance has started and not yet ended."""
        return self._speaking

    def reset(self) -> None:
        """Drop everything buffered, including the VAD's recurrent state."""
        self._clear()
        self._reset_vad()

    def feed(self, chunk: Int16Array) -> Int16Array | None:
        """Consume *chunk*; return the utterance it completed, or `None`."""
        buffer = np.concatenate([self._carry, chunk]) if len(self._carry) else chunk
        n_frames = len(buffer) // FRAME_SAMPLES
        self._carry = buffer[n_frames * FRAME_SAMPLES :].astype(np.int16)
        for i in range(n_frames):
            utterance = self._feed_frame(buffer[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES])
            if utterance is not None:
                # Audio right after the boundary may open the next utterance.
                self._carry = buffer[(i + 1) * FRAME_SAMPLES :].astype(np.int16)
                return utterance
        return None

    def _feed_frame(self, frame: Int16Array) -> Int16Array | None:
        prob = self._vad(frame)
        if not self._speaking:
            if prob < self._start_threshold:
                self._pre_roll.append(frame)
                self._pre_roll_total += len(frame)
                while self._pre_roll_total > self._pre_roll_limit and self._pre_roll:
                    self._pre_roll_total -= len(self._pre_roll.popleft())
                return None
            self._speaking = True
            self._speech = [*self._pre_roll, frame]
            self._speech_total = self._pre_roll_total + len(frame)
            self._pre_roll.clear()
            self._pre_roll_total = 0
            self._silence_run = 0
        else:
            self._speech.append(frame)
            self._speech_total += len(frame)
            self._silence_run = 0 if prob >= self._end_threshold else self._silence_run + len(frame)
        if self._silence_run >= self._silence_limit or self._speech_total >= self._max_samples:
            return self._finish()
        return None

    def _finish(self) -> Int16Array:
        utterance = np.concatenate(self._speech)
        _LOG.info("vad: utterance ended (%.1fs)", self._speech_total / SAMPLE_RATE)
        self._speech = []
        self._speech_total = 0
        self._speaking = False
        self._silence_run = 0
        self._reset_vad()
        return utterance

    def _reset_vad(self) -> None:
        reset = getattr(self._vad, "reset", None)
        if reset is not None:
            reset()


__all__ = ["FRAME_SAMPLES", "SAMPLE_RATE", "Int16Array", "SileroVad", "UtteranceSegmenter"]
