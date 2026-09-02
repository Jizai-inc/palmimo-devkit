"""Silero VAD v5 wrapper and a hardware-free utterance segmenter.

:class:`SileroVad` wraps an ``onnxruntime`` session over the Silero VAD v5
ONNX model, auto-downloading it into the shared Palmimo model cache (see
:mod:`palmimo_sdk.download`) the same way :mod:`palmimo_sdk.audio.denoise`
resolves the GTCRN model.

:class:`UtteranceSegmenter` is the pure, hardware-free state machine that
turns a stream of speech probabilities into complete utterances: it never
touches ``onnxruntime`` or ``numpy`` beyond array math, so it is exercised in
tests against a scripted fake VAD.

This module deliberately mirrors ``palmimo_wakeword_agent.vad`` (same model,
same state machine) rather than importing it -- each example under
``examples/agents/`` stays self-contained, so the two are never cross-wired.
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


_log = logging.getLogger(__name__)

#: A mono int16 PCM audio buffer (one frame or one full utterance).
type Int16Array = npt.NDArray[np.int16]

#: Frame size the Silero VAD v5 ONNX model expects (32 ms at 16 kHz), matching
#: :data:`palmimo_sdk.io.mic_stream.BLOCKSIZE` -- one MicStream chunk is one
#: VAD frame, no internal rebuffering needed for the SDK's own capture path
#: (this segmenter still rebuffers defensively for any other chunk size).
FRAME_SAMPLES = 512

#: Silero VAD v5.1.2 (snakers4/silero-vad), the raw ONNX asset from the
#: upstream repo's tagged release.
SILERO_VAD_MODEL_URL = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/src/silero_vad/data/silero_vad.onnx"
)
#: sha256 of the file at SILERO_VAD_MODEL_URL, verified by downloading it and
#: hashing it directly (see the wakeword example's development notes).
SILERO_VAD_MODEL_SHA256 = "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f"

_MODEL_FILENAME = "silero_vad.onnx"

#: How long an idle window accumulates (in seconds' worth of samples) before
#: :class:`UtteranceSegmenter` considers logging a near-miss summary. A
#: totally quiet room never reaches :data:`_IDLE_SUMMARY_MIN_PROB`, so this
#: mostly bounds how often a genuinely noisy-but-below-threshold room logs.
_IDLE_SUMMARY_SECONDS = 5.0

#: Minimum max-probability seen in an idle window for the near-miss summary
#: to actually log -- below this, the room is quiet enough that logging would
#: just be spam.
_IDLE_SUMMARY_MIN_PROB = 0.30


def _default_model_dir() -> Path:
    """Return the shared Palmimo model cache directory (``~/.cache/palmimo/models``)."""
    from palmimo_sdk.audio import default_model_dir

    return default_model_dir()


class OnnxSessionLike(Protocol):
    """Minimal structural surface of ``onnxruntime.InferenceSession`` we depend on."""

    def run(
        self,
        output_names: Sequence[str] | None,
        input_feed: Mapping[str, npt.NDArray[np.floating] | npt.NDArray[np.int64]],
    ) -> Sequence[npt.NDArray[np.float32]]: ...


def session_options(ort: Any) -> Any:
    """Build the ``onnxruntime.SessionOptions`` the VAD model is run under.

    Silero VAD v5 is a ~2 MB graph invoked once per 32 ms frame -- 31 inferences
    a second, each far too small to be worth parallelising. Left at the
    defaults, ONNX Runtime sizes its intra-op pool to the core count and lets
    those threads *spin* between inferences, so the pool burns cores waiting for
    work that arrives 32 ms apart.

    Measured on a Raspberry Pi 5, capturing from a real mic for 60 s and running
    the identical 1875 frames through the identical graph: with the defaults,
    242% of one core; with the options built here, 2.5%.

    Takes *ort* as an argument rather than importing it, so the policy can be
    asserted without the caller having pulled in ``onnxruntime``.
    """
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    # No SessionOptions attribute covers spinning; it is a config entry.
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    return options


def _new_inference_session(path: str) -> OnnxSessionLike:
    """Build a real ``onnxruntime.InferenceSession`` over *path*.

    Its own function (rather than inlined in :meth:`SileroVad.load`) so tests
    can monkeypatch it and never import ``onnxruntime``.
    """
    import onnxruntime as ort  # lazy: keeps import-time light without the voice extra

    # onnxruntime's own stubs type `run` loosely (a union covering sparse
    # tensors and pickled outputs); for the Silero VAD graph it always returns
    # float32 arrays, so narrow to the protocol here instead of widening it.
    return cast("OnnxSessionLike", ort.InferenceSession(path, sess_options=session_options(ort)))


class SileroVad:
    """Silero VAD v5 speech-probability model over an onnxruntime session.

    Verified against the real model file: inputs ``input`` (float32, one
    frame of :data:`FRAME_SAMPLES` normalized samples), ``state`` (float32,
    recurrent state, shape ``(2, 1, 128)``), ``sr`` (int64 scalar, sample
    rate); outputs ``output`` (speech probability) and ``stateN`` (the next
    recurrent state).
    """

    def __init__(self, session: OnnxSessionLike, *, sample_rate: int = 16000) -> None:
        self._session = session
        self._sample_rate = sample_rate
        self._state: npt.NDArray[np.float32] = np.zeros((2, 1, 128), dtype=np.float32)

    @classmethod
    def load(cls, model_path: Path | None = None) -> SileroVad:
        """Build a :class:`SileroVad`, downloading the model if missing.

        Args:
            model_path: Explicit ONNX file path. ``None`` defaults to the
                shared Palmimo model cache directory.

        Returns:
            A ready-to-use :class:`SileroVad`.
        """
        resolved = model_path if model_path is not None else _default_model_dir() / _MODEL_FILENAME
        if not resolved.exists():
            download_atomic(SILERO_VAD_MODEL_URL, resolved, timeout=60.0, sha256=SILERO_VAD_MODEL_SHA256)
        session = _new_inference_session(str(resolved))
        return cls(session)

    def reset(self) -> None:
        """Clear the recurrent state, starting a fresh sequence."""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def __call__(self, frame: Int16Array) -> float:
        """Return the speech probability for one :data:`FRAME_SAMPLES` int16 frame."""
        samples = frame.astype(np.float32) / 32768.0
        inputs: dict[str, npt.NDArray[np.floating] | npt.NDArray[np.int64]] = {
            "input": samples[None, :],
            "state": self._state,
            "sr": np.array(self._sample_rate, dtype=np.int64),
        }
        output, next_state = self._session.run(None, inputs)
        self._state = next_state
        return float(output[0][0])


class VadCallable(Protocol):
    """Protocol an :class:`UtteranceSegmenter` VAD dependency must satisfy."""

    def __call__(self, frame: Int16Array) -> float: ...


class UtteranceSegmenter:
    """Segments utterances from a stream of arbitrary-length int16 chunks.

    Pure and hardware-free: the only external dependency is the *vad*
    callable passed in, so it never opens a microphone or loads a model
    itself. Internally rebuffers whatever is :meth:`feed`\\ d into exact
    :data:`FRAME_SAMPLES`-sample frames (carrying any remainder to the next
    call) before calling *vad*, matching Silero VAD v5's fixed window.

    State machine: while idle, frames below ``start_threshold`` are kept in a
    bounded pre-roll buffer (``pre_roll_seconds``) so a crossing frame's
    utterance is not clipped at the start. Once ``start_threshold`` is
    crossed, the utterance begins (prefixed by the pre-roll). While speaking,
    a probability at or above ``end_threshold`` resets the silence run;
    sustained silence (``silence_seconds``) or reaching
    ``max_utterance_seconds`` completes the utterance.

    Args:
        vad: Speech-probability callable, one :data:`FRAME_SAMPLES`-sample
            int16 frame in, one float out. If it exposes a ``reset()``
            method, it is called whenever an utterance completes (also
            checked with ``hasattr`` -- a plain callable with no ``reset()``
            is tolerated).
        sample_rate: Audio sample rate in Hz.
        start_threshold: Probability that starts an utterance from idle.
        end_threshold: Probability at/above which the in-utterance silence
            run resets.
        silence_seconds: Sustained low-probability duration that completes
            an in-progress utterance.
        max_utterance_seconds: Hard cap on utterance duration.
        pre_roll_seconds: Idle audio retained immediately before a crossing,
            prefixed onto the utterance that follows.
    """

    def __init__(
        self,
        vad: Callable[[Int16Array], float],
        sample_rate: int = 16000,
        start_threshold: float = 0.5,
        end_threshold: float = 0.35,
        silence_seconds: float = 0.8,
        max_utterance_seconds: float = 15.0,
        pre_roll_seconds: float = 0.3,
    ) -> None:
        self._vad = vad
        self._sample_rate = sample_rate
        self._start_threshold = start_threshold
        self._end_threshold = end_threshold
        self._silence_limit = int(silence_seconds * sample_rate)
        self._max_samples = int(max_utterance_seconds * sample_rate)
        self._pre_roll_limit = int(pre_roll_seconds * sample_rate)
        self._idle_summary_limit = int(_IDLE_SUMMARY_SECONDS * sample_rate)
        self._reset_state()

    def _reset_state(self) -> None:
        self._carry: Int16Array = np.empty(0, dtype=np.int16)
        self._pre_roll: collections.deque[Int16Array] = collections.deque()
        self._pre_roll_total = 0
        self._speech: list[Int16Array] = []
        self._speech_total = 0
        self._speaking = False
        self._silence_run = 0
        self._reset_idle_window()

    def _reset_idle_window(self) -> None:
        """Clear the idle near-miss summary's running counters."""
        self._idle_window_samples = 0
        self._idle_max_prob = 0.0
        self._idle_above_end_threshold_count = 0

    def reset(self) -> None:
        """Drop all buffered state (carry, pre-roll, in-progress utterance).

        Also resets the VAD's recurrent state (like the completion path in
        :meth:`_finalize` does) -- otherwise model context from the abandoned
        audio would bias probabilities for whatever comes next.
        """
        self._reset_state()
        reset = getattr(self._vad, "reset", None)
        if reset is not None:
            reset()

    def feed(self, chunk: Int16Array) -> Int16Array | None:
        """Consume an arbitrary-length int16 *chunk*; return a completed utterance, or ``None``.

        Args:
            chunk: 1-D int16 samples of any length.

        Returns:
            The concatenated int16 utterance once it completes, otherwise
            ``None``.
        """
        buffer = np.concatenate([self._carry, chunk]) if len(self._carry) else chunk
        n_frames = len(buffer) // FRAME_SAMPLES
        remainder_start = n_frames * FRAME_SAMPLES
        self._carry = buffer[remainder_start:].copy().astype(np.int16)

        for i in range(n_frames):
            frame = buffer[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]
            result = self._feed_frame(frame)
            if result is not None:
                # Keep the not-yet-scored tail of this chunk (later frames plus
                # the sub-frame remainder) so audio right after an utterance
                # boundary is not lost -- it may open the next utterance.
                self._carry = buffer[(i + 1) * FRAME_SAMPLES :].copy().astype(np.int16)
                return result
        return None

    def _feed_frame(self, frame: Int16Array) -> Int16Array | None:
        prob = self._vad(frame)
        if not self._speaking:
            self._update_idle_window(prob, len(frame))
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
            self._reset_idle_window()
            _log.info("vad: utterance started (prob=%.2f)", prob)
        else:
            self._speech.append(frame)
            self._speech_total += len(frame)
            if prob >= self._end_threshold:
                self._silence_run = 0
            else:
                self._silence_run += len(frame)

        if self._silence_run >= self._silence_limit:
            return self._finalize("silence")
        if self._speech_total >= self._max_samples:
            return self._finalize("max-cap")
        return None

    def _update_idle_window(self, prob: float, frame_samples: int) -> None:
        """Accumulate the idle near-miss summary's window and log it once it fills.

        Tracks the loudest probability seen and how many frames reached
        ``end_threshold`` while idle; once at least :data:`_IDLE_SUMMARY_SECONDS`
        of idle audio has accumulated (by sample count, never a wall clock --
        this class stays hardware-free), logs a summary if the window ever got
        close to starting an utterance, otherwise resets silently so a quiet
        room produces no log spam.
        """
        self._idle_window_samples += frame_samples
        self._idle_max_prob = max(self._idle_max_prob, prob)
        if prob >= self._end_threshold:
            self._idle_above_end_threshold_count += 1
        if self._idle_window_samples < self._idle_summary_limit:
            return
        if self._idle_max_prob >= _IDLE_SUMMARY_MIN_PROB:
            _log.info(
                "vad: idle %.0fs, max_prob=%.2f (start_threshold=%.2f), frames>=end_threshold: %d",
                self._idle_window_samples / self._sample_rate,
                self._idle_max_prob,
                self._start_threshold,
                self._idle_above_end_threshold_count,
            )
        self._reset_idle_window()

    def _finalize(self, reason: str) -> Int16Array:
        """Complete the in-progress utterance and return its concatenated audio.

        Args:
            reason: Why the utterance ended -- ``"silence"`` (sustained
                low-probability run) or ``"max-cap"`` (hit
                ``max_utterance_seconds``), logged alongside the duration.
        """
        duration_seconds = self._speech_total / self._sample_rate
        utterance = np.concatenate(self._speech)
        self._speech = []
        self._speech_total = 0
        self._speaking = False
        self._silence_run = 0
        _log.info("vad: utterance finalized (%.1fs, %s)", duration_seconds, reason)
        reset = getattr(self._vad, "reset", None)
        if reset is not None:
            reset()
        return utterance
