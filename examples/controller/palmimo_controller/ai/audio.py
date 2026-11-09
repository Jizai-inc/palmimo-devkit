"""Microphone in, synthesized speech out: the two audio ends of an AI mode.

`Listener` runs on its own thread: it reads mic chunks, cuts them into
utterances, and decides what the robot may hear while it is talking.
`Voice` wraps the SDK's `Speaker` so that thread and the event loop can both
ask whether the robot is speaking.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np
from scipy.signal import resample_poly

from .vad import SAMPLE_RATE, Int16Array


#: A bounded read, so a stop request is noticed within this long even when the mic is silent.
_MIC_READ_TIMEOUT_S = 0.2

#: Without echo cancellation the room still rings briefly after playback ends;
#: listening again any sooner lets the robot hear the end of its own sentence.
_ECHO_TAIL_S = 0.4


class MicStreamLike(Protocol):
    """The part of `palmimo_sdk.MicStream` an AI mode reads."""

    sample_rate: int

    def stream(self) -> Any: ...


class SpeechHandleLike(Protocol):
    def is_alive(self) -> bool: ...


class SpeakerLike(Protocol):
    """The part of `palmimo_sdk.Speaker` an AI mode uses. `say` never blocks."""

    def say(self, text: str) -> SpeechHandleLike: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


class SegmenterLike(Protocol):
    """The part of `vad.UtteranceSegmenter` the listener drives."""

    @property
    def speaking(self) -> bool: ...

    def feed(self, chunk: Int16Array) -> Int16Array | None: ...

    def reset(self) -> None: ...


def to_pipeline_rate(chunk: Any, rate: int) -> Int16Array:
    """Return *chunk* as int16 at `SAMPLE_RATE`; the booth's USB mic captures at 48 kHz."""
    samples = np.asarray(chunk)
    if rate == SAMPLE_RATE:
        return samples.astype(np.int16, copy=False)
    common = math.gcd(SAMPLE_RATE, rate)
    resampled = resample_poly(samples.astype(np.float32), SAMPLE_RATE // common, rate // common)
    return np.clip(resampled, -32768, 32767).astype(np.int16)


class Voice:
    """The robot's speaker, plus whether it is talking right now. Safe to call from any thread."""

    def __init__(self, speaker: SpeakerLike, *, now: Callable[[], float] = time.monotonic) -> None:
        self._speaker = speaker
        self._now = now
        self._lock = threading.Lock()
        self._handle: SpeechHandleLike | None = None
        self._quiet_since: float | None = None

    def say(self, text: str) -> None:
        with self._lock:
            self._handle = self._speaker.say(text)

    def interrupt(self) -> None:
        """Cut the robot off mid-sentence."""
        with self._lock:
            self._handle = None
            self._quiet_since = self._now()
        self._speaker.stop()

    def speaking(self, *, tail: float = 0.0) -> bool:
        """Whether speech is playing, or ended less than *tail* seconds ago."""
        with self._lock:
            if self._handle is not None and not self._handle.is_alive():
                self._handle = None
                self._quiet_since = self._now()
            if self._handle is not None:
                return True
            return self._quiet_since is not None and self._now() - self._quiet_since < tail

    def close(self) -> None:
        self._speaker.stop()
        self._speaker.close()


class Listener:
    """Mic → utterances, on a worker thread, handing each one to *on_utterance*.

    With echo cancellation, speech that starts while the robot talks cuts it
    off (barge-in). Without it the robot would hear itself, so the mic is
    ignored until the robot has finished talking.
    """

    def __init__(
        self,
        mic: MicStreamLike,
        segmenter: SegmenterLike,
        voice: Voice,
        on_utterance: Callable[[Int16Array], None],
        *,
        barge_in: bool,
    ) -> None:
        self._mic = mic
        self._segmenter = segmenter
        self._voice = voice
        self._on_utterance = on_utterance
        self._barge_in = barge_in
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        with self._mic.stream() as chunks:
            while not self._stop.is_set():
                chunk = chunks.get(_MIC_READ_TIMEOUT_S)
                if chunk is None:
                    continue
                self._hear(to_pipeline_rate(chunk, self._mic.sample_rate))

    def _hear(self, pcm: Int16Array) -> None:
        if not self._barge_in and self._voice.speaking(tail=_ECHO_TAIL_S):
            self._segmenter.reset()
            return
        was_speaking = self._segmenter.speaking
        utterance = self._segmenter.feed(pcm)
        if self._barge_in and not was_speaking and self._segmenter.speaking and self._voice.speaking():
            self._voice.interrupt()
        if utterance is not None:
            self._on_utterance(utterance)


__all__ = ["Listener", "MicStreamLike", "SegmenterLike", "SpeakerLike", "Voice", "to_pipeline_rate"]
