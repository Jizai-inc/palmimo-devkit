"""SpeechPipeline -- mic chunks -> VAD segmentation -> STT -> guard -> the conductor's bus.

Ties a :class:`~palmimo_sdk.MicStream`-compatible source,
:class:`~palmimo_companion_agent.pipeline.vad.UtteranceSegmenter`,
:class:`~palmimo_companion_agent.pipeline.llm.LlmProvider`'s ``transcribe`` /
``classify_speech``, and :class:`~palmimo_companion_agent.pipeline.conductor.Conductor`'s
``submit_speech`` into one loop: subscribe to the mic, feed each chunk to the
segmenter, transcribe and guard-classify each completed utterance, and post the
verdict to the conductor. Everything upstream of that (deciding whether a
verdict cancels an in-flight tool, becomes history, ...) already lives in
:mod:`palmimo_companion_agent.pipeline.conductor` -- this module's only job is turning
raw audio into a guarded ``(verdict, text)`` pair.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from palmimo_sdk.audio import int16_to_wav

from .llm import SpeechVerdict
from .vad import SileroVad, UtteranceSegmenter


if TYPE_CHECKING:
    from .vad import Int16Array


_log = logging.getLogger(__name__)

#: Sample rate the pipeline assumes throughout: the segmenter's Silero VAD v5
#: window, the SDK's MicStream default, and the WAV bytes handed to STT are
#: all pinned to this rate -- see the module docstrings in
#: palmimo_sdk.io.mic_stream and .vad for why 16 kHz / 512-sample chunks are
#: the SDK's own contract, not just this pipeline's choice.
SAMPLE_RATE_HZ = 16000

#: How long a single blocking Subscription.get() call waits before returning
#: control to the event loop. Bounds how long run()'s to_thread call can sit
#: past a task cancellation (the underlying thread only unblocks at this
#: cadence), while still keeping polling cheap.
_SUBSCRIPTION_POLL_SECONDS = 0.5

#: Completed utterances waiting on STT/guard. The mic-read loop never awaits
#: those network calls itself (they take seconds, while the subscription's
#: drop-oldest buffer holds far less), so finished utterances queue here for
#: a worker task; when even this backs up, the OLDEST utterance is dropped --
#: newer speech is what the user is waiting on.
_UTTERANCE_QUEUE_MAX = 4


class SubscriptionLike(Protocol):
    """Structural surface of a :class:`~palmimo_sdk.Subscription` this pipeline depends on."""

    #: Count of chunks the subscription's drop-oldest buffer has discarded
    #: because this pipeline was not draining it fast enough -- see
    #: :attr:`palmimo_sdk.io.mic_stream.Subscription.dropped`.
    dropped: int

    def get(self, timeout: float | None = None) -> Int16Array | None: ...
    def close(self) -> None: ...


class MicStreamLike(Protocol):
    """Structural surface of a :class:`~palmimo_sdk.MicStream` this pipeline depends on."""

    def stream(self) -> SubscriptionLike: ...


class SpeechLlmLike(Protocol):
    """The :class:`~palmimo_companion_agent.pipeline.llm.LlmProvider` capabilities this pipeline calls."""

    async def transcribe(self, audio_bytes: bytes, *, language: str = ..., filename: str = ...) -> str: ...
    async def classify_speech(self, text: str) -> SpeechVerdict: ...


class SpeechConductorLike(Protocol):
    """The :class:`~palmimo_companion_agent.pipeline.conductor.Conductor` surface this pipeline calls."""

    def submit_speech(self, verdict: SpeechVerdict, text: str) -> None: ...


class SegmenterLike(Protocol):
    """Structural surface of :class:`~palmimo_companion_agent.pipeline.vad.UtteranceSegmenter` used here."""

    def feed(self, chunk: Int16Array) -> Int16Array | None: ...


def _build_default_segmenter(silence_seconds: float | None = None) -> UtteranceSegmenter:
    """Build the production segmenter: a real Silero VAD over its default model cache.

    *silence_seconds* overrides how long the talker has to stop before the
    utterance is considered finished -- dead time on the front of every reply,
    traded against clipping people who pause mid-sentence. None leaves the
    segmenter's own default, so this stays a single source of truth.
    """
    options = {} if silence_seconds is None else {"silence_seconds": silence_seconds}
    return UtteranceSegmenter(vad=SileroVad.load(), sample_rate=SAMPLE_RATE_HZ, **options)


class SpeechPipeline:
    """Drains a mic subscription into guarded ``(verdict, text)`` posts on the conductor.

    Args:
        mic: A :class:`~palmimo_sdk.MicStream`-compatible source (only
            :meth:`stream` is used). Its chunks are expected to already be
            echo-cancelled, 16 kHz, mono int16 -- MicStream's own default (see
            :mod:`palmimo_sdk.io.mic_stream`).
        llm: Provides ``transcribe`` (STT) and ``classify_speech`` (the guard).
        conductor: Receives each guarded utterance via ``submit_speech``.
        language: ISO 639-1 STT language hint, forwarded to ``llm.transcribe``.
        segmenter: The utterance segmenter chunks are fed through. ``None``
            (the default) builds a real :class:`~palmimo_companion_agent.pipeline.vad.UtteranceSegmenter`
            over a :class:`~palmimo_companion_agent.pipeline.vad.SileroVad` loaded from the
            shared Palmimo model cache (downloading it on first use). Tests
            inject a segmenter built over a scripted fake VAD instead, so no
            ONNX model or real microphone is ever touched off the hardware path.
        silence_seconds: How long the talker must stop before the utterance
            is treated as finished. None uses the segmenter's own default.
            Ignored when *segmenter* is given -- that one is already built.
    """

    def __init__(
        self,
        *,
        mic: MicStreamLike,
        llm: SpeechLlmLike,
        conductor: SpeechConductorLike,
        language: str = "ja",
        segmenter: SegmenterLike | None = None,
        silence_seconds: float | None = None,
        on_transcript: Callable[[str], None] | None = None,
    ) -> None:
        self._mic = mic
        self._llm = llm
        self._conductor = conductor
        self._language = language
        self._on_transcript = on_transcript
        self._segmenter = segmenter if segmenter is not None else _build_default_segmenter(silence_seconds)
        # The whole pipeline (Silero VAD's 512-sample window, the WAV header
        # handed to STT) is pinned to 16 kHz; a mic opened at another rate
        # would transcribe at the wrong speed, so fail at construction, not
        # after the first garbled utterance.
        mic_rate = getattr(mic, "sample_rate", SAMPLE_RATE_HZ)
        if mic_rate != SAMPLE_RATE_HZ:
            raise ValueError(f"mic sample rate is {mic_rate} Hz; this pipeline requires {SAMPLE_RATE_HZ} Hz")

    async def run(self) -> None:
        """Subscribe to the mic and process utterances until this task is cancelled.

        The read loop only reads, segments, and enqueues: STT/guard calls
        take seconds, and awaiting them inline would stall the subscription
        (whose drop-oldest buffer holds well under a second of audio) and
        clip any speech arriving meanwhile. Completed utterances go to a
        bounded queue a worker task drains -- see :data:`_UTTERANCE_QUEUE_MAX`
        for the overflow policy.

        Reads use a bounded poll timeout via :func:`asyncio.to_thread`, so
        the event loop stays responsive to cancellation between reads;
        segmenting a chunk (the VAD's ONNX inference) is likewise off-loaded.
        A ``None`` read is either a poll timeout or the stream closing --
        indistinguishable from the call alone, so the mic's ``is_open`` (when
        it has one) decides between polling on and exiting. On any exit, the
        subscription is closed and the worker cancelled in ``finally``.

        Every successful read also compares the subscription's cumulative
        ``dropped`` counter against its last-seen value, warning when it grew
        -- a slow reader losing chunks to the drop-oldest buffer would
        otherwise be silent (see :attr:`SubscriptionLike.dropped`).
        """
        sub = self._mic.stream()
        utterances: asyncio.Queue[Int16Array] = asyncio.Queue(maxsize=_UTTERANCE_QUEUE_MAX)
        worker = asyncio.ensure_future(self._utterance_worker(utterances))
        last_dropped = 0
        try:
            while True:
                chunk = await asyncio.to_thread(sub.get, _SUBSCRIPTION_POLL_SECONDS)
                if chunk is None:
                    if not getattr(self._mic, "is_open", True):
                        _log.warning("mic stream closed; speech pipeline exiting")
                        return
                    continue  # a poll timeout; keep reading.
                sub_dropped = sub.dropped
                if sub_dropped > last_dropped:
                    _log.warning(
                        "mic subscription dropped %d chunk(s) (total %d)", sub_dropped - last_dropped, sub_dropped
                    )
                last_dropped = sub_dropped
                utterance = await asyncio.to_thread(self._segmenter.feed, chunk)
                if utterance is None:
                    continue
                while True:
                    try:
                        utterances.put_nowait(utterance)
                        break
                    except asyncio.QueueFull:
                        dropped = utterances.get_nowait()
                        _log.warning("utterance queue full; dropped a %d-sample utterance", len(dropped))
        finally:
            sub.close()
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    async def _utterance_worker(self, utterances: asyncio.Queue[Int16Array]) -> None:
        """Drain completed utterances through STT/guard, decoupled from mic reads."""
        while True:
            utterance = await utterances.get()
            await self._handle_utterance(utterance)

    async def _handle_utterance(self, utterance: Int16Array) -> None:
        """Transcribe, guard-classify, and submit one completed utterance.

        A transcription failure is logged and the utterance dropped -- there
        is no text to act on. A guard failure falls back to treating the
        transcription as a command (per :meth:`~palmimo_companion_agent.pipeline.llm.LlmProvider.classify_speech`'s
        contract): the user did say something, and losing real speech to a
        transient guard outage is worse than letting the odd hallucination
        through. An empty transcription (silence) is dropped without even
        reaching the guard. Every drop (empty transcription, transcribe
        failure, classify failure) is logged, so an utterance's fate is
        always visible in the logs, not silent.
        """
        wav_bytes = int16_to_wav(utterance, SAMPLE_RATE_HZ)
        try:
            text = await self._llm.transcribe(wav_bytes, language=self._language)
        except Exception as exc:
            _log.warning("transcribe failed, discarding utterance: %s", exc)
            return
        _log.info("stt (%.1fs audio): %r", len(utterance) / SAMPLE_RATE_HZ, text)
        self._notify_transcript(text)
        if not text.strip():
            _log.info("stt empty; dropping %.1fs utterance", len(utterance) / SAMPLE_RATE_HZ)
            return
        try:
            verdict = await self._llm.classify_speech(text)
        except Exception as exc:
            _log.warning("classify_speech failed, falling back to command: %s", exc)
            verdict = SpeechVerdict(kind="command", text=None)
        self._conductor.submit_speech(verdict, text)

    def set_transcript_observer(self, observer: Callable[[str], None] | None) -> None:
        """Set (or clear) the observer handed every transcript.

        A setter as well as a constructor argument because the observer is
        wired after construction: the reflex layer that consumes transcripts is
        built from this pipeline's own presence, so it cannot exist yet when
        this is constructed.
        """
        self._on_transcript = observer

    def _notify_transcript(self, text: str) -> None:
        """Hand the raw transcript to the optional observer, before the guard.

        Before the guard on purpose: the observer this exists for is the
        name-call reflex, and being called is worth answering even when
        classify_speech goes on to rule the utterance out as chatter. A failing
        observer costs its own reaction and nothing else -- the utterance
        continues to the conductor either way.
        """
        if self._on_transcript is None:
            return
        try:
            self._on_transcript(text)
        except Exception:
            _log.warning("transcript observer failed", exc_info=True)


__all__ = [
    "SAMPLE_RATE_HZ",
    "MicStreamLike",
    "SegmenterLike",
    "SpeechConductorLike",
    "SpeechLlmLike",
    "SpeechPipeline",
    "SubscriptionLike",
]
