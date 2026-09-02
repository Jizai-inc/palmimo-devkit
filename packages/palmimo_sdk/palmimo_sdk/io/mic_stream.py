# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""MicStream — a shared, background-owned microphone capture.

Mirrors :class:`~palmimo_sdk.io.camera.HeadCamera`'s background drain, but for
audio: a physical mic can only be opened by one ``sounddevice.InputStream` at
a time, yet several consumers (an always-on VAD / ambient-sound reflex, an
on-demand ASR capture, ...) may all want the same raw audio. :class:`MicStream`
opens the device exactly once, reads it continuously on a background thread,
and fans each small chunk out to every :class:`Subscription`.

Format matches the rest of the voice pipeline by default: 16k / mono / int16 —
``sample_rate`` can be changed, but the default ``processors`` (an
:class:`~palmimo_sdk.audio.aec.EchoCanceller`) is fixed at 16k, so
:meth:`MicStream.open` fails fast with a clear error on a mismatch instead of
every chunk silently failing to process on the background thread (see
:meth:`MicStream.open`). Chunking at
``BLOCKSIZE`` (512 samples = 32 ms at 16k) matches Silero VAD v5's window, so a
VAD consumer needs no internal buffering — but only when no ``processors`` are
in the way (see :class:`MicStream`'s ``processors`` parameter): a processor
chain buffers/reshapes chunks internally, so that one-chunk-one-window
assumption only holds for ``processors=[]`` (raw audio).

The read-failure backoff and the "don't clear the handle on a join timeout"
rule in :meth:`close` both encode hardware lessons — see the docstrings
below for why.

``sounddevice`` / ``numpy`` (the ``palmimo-sdk[voice]`` extra) are imported
lazily inside :meth:`open` / :meth:`record`, so ``import palmimo_sdk`` and
``import palmimo_sdk.io`` stay dependency-free for compute-only use, matching
every other resource in this package.

Device identity across :class:`MicStream` and
:class:`~palmimo_sdk.io.microphone.Microphone` is the ``device_key`` string —
see :mod:`palmimo_sdk.io._mic_registry` for the convention and its limits.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING, Any

from ..audio.processor import int16_to_wav, wav_to_int16
from . import _mic_registry


if TYPE_CHECKING:
    from ..audio.processor import AudioProcessor


#: Samples per chunk. 512 at 16k = 32 ms, matching Silero VAD v5's window (a
#: VAD consumer can treat one chunk as one inference, no buffering needed).
#: Kept as a single named constant rather than a literal repeated at call
#: sites, so a future retune can't change it in one place and not the other.
BLOCKSIZE = 512

#: A block counts as "late" once processing it took over this fraction of the
#: real time it represents (`elapsed > LATE_BLOCK_FRACTION * budget`, where
#: `budget = frames / sample_rate`) — a signal the capture thread is falling
#: behind (DTLN-AEC inference runs on this thread, and a slow host, e.g. a
#: Pi, can lose samples with zero visibility otherwise).
LATE_BLOCK_FRACTION = 0.8

#: Default bound on a Subscription's queue (see Subscription docstring for why
#: bounded / drop-oldest, not unbounded).
_DEFAULT_SUB_MAXSIZE = 64

#: How long close() waits for the background thread to notice the stop signal
#: and exit before giving up on a clean stream release (see close() docstring).
_JOIN_TIMEOUT_S = 2.0

#: record()'s wait budget beyond the requested duration, for a stream that
#: stops delivering chunks (device pulled, etc.) — mirrors Microphone.record's
#: subprocess timeout margin.
_RECORD_TIMEOUT_MARGIN_S = 5.0

#: Sentinel dispatched to every Subscription's queue when the stream closes,
#: so a `for chunk in stream.stream()` loop (or Subscription.__next__) ends
#: instead of blocking forever.
_CLOSE_SENTINEL = None

InputStreamFactory = Callable[[int, int, str, int, Any], Any]


class Subscription:
    """One consumer's view onto a :class:`MicStream`'s chunk feed.

    Backed by a bounded queue: the stream writes with drop-oldest (never
    blocks the capture thread on a slow consumer), and :attr:`dropped` counts
    how many chunks that consumer lost this way — a slow VAD losing chunks
    silently would otherwise be invisible.

    After the owning stream closes, :meth:`get` returns ``None`` and iteration
    (``for chunk in sub`` / ``next(sub)``) stops via ``StopIteration`` — the
    same sentinel-based termination :meth:`MicStream.close` uses everywhere.

    Every chunk delivered here is read-only (``chunk.flags.writeable is
    False``) regardless of whether it came from the raw path or a
    ``processors`` chain — the same chunk object is fanned out to every
    subscriber, so one subscriber mutating it in place would corrupt what the
    others see. Call ``chunk.copy()`` first if you need to modify it.

    Usable as a context manager — this is the recommended way to consume a
    subscription: ``__enter__`` returns ``self``, ``__exit__`` calls
    :meth:`close` (unsubscribe). Not unsubscribing when done (e.g. dropping a
    subscription without calling :meth:`close`) leaves it registered — the
    capture thread keeps writing to (and eventually drop-oldest-ing into) its
    queue for as long as the stream stays open, wasted work for a subscriber
    nobody is draining anymore::

        with mic.stream() as chunks:
            for chunk in chunks:
                ...
    """

    def __init__(self, owner: MicStream, maxsize: int = _DEFAULT_SUB_MAXSIZE) -> None:
        self._owner = owner
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=maxsize)
        self.dropped = 0

    def get(self, timeout: float | None = None) -> Any:
        """Return the next chunk, or ``None`` if the stream closed / *timeout* elapsed.

        With ``timeout=None`` (default), blocks until a chunk arrives or the
        stream closes.

        A ``None`` here means either "closed" or "timed out" — those are
        indistinguishable from this call alone. Rarely, a chunk queued right
        at close time can be delivered right after the sentinel that was
        already sent for a slow join (see :meth:`MicStream.close`); calling
        :meth:`get` again would see it. This doesn't affect iteration
        (``for chunk in sub`` / ``next(sub)``), which stops at the first
        sentinel and never asks again.
        """
        try:
            if timeout is None:
                return self._queue.get()
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        item = self._queue.get()
        if item is _CLOSE_SENTINEL:
            raise StopIteration
        return item

    def close(self) -> None:
        """Unsubscribe from the owning stream. Same as ``MicStream.unsubscribe(self)``. Idempotent."""
        self._owner.unsubscribe(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class MicStream:
    """Owns one microphone ``InputStream``, read continuously on a background thread.

    Lifecycle: :meth:`open` (idempotent) / :meth:`close` (idempotent). Usable
    as a context manager. Consumers attach via :meth:`subscribe` (fan-out,
    each gets every chunk) or the :meth:`record` convenience for a bounded
    WAV capture.

    Parameters
    ----------
    sample_rate : int
        Capture rate in Hz.
    blocksize : int
        Samples per chunk read from the device (see :data:`BLOCKSIZE`).
    device_name_hint : str, optional
        Case-insensitive substring match against input device names (e.g.
        ``"ReSpeaker"``), used only on the real ``sounddevice`` path — a
        mount-independent alternative to a fixed device index/ALSA string.
        ``None`` uses the default input device.
    device_key : str
        Identity used to register this stream in :mod:`_mic_registry` and to
        coordinate with a :class:`~palmimo_sdk.io.microphone.Microphone` on
        the same physical mic. See that module's docstring for the (fail-fast,
        convention-based, single-process) contract.
    input_stream_factory : callable, optional
        Test seam: ``factory(samplerate, channels, dtype, blocksize, device)
        -> stream``, where ``stream`` exposes ``start()`` / ``read(n) ->
        (ndarray, overflowed)`` / ``stop()`` / ``close()``. ``None`` (default)
        lazily imports ``sounddevice`` and builds a real ``InputStream`` at
        :meth:`open` time, resolving *device_name_hint* via
        ``sounddevice.query_devices()``. When a factory IS given, device
        resolution is skipped (there is no real device list to resolve
        against) and *device_name_hint* is passed straight through as the
        ``device`` argument — the factory owns interpreting it.
    processors : Sequence[AudioProcessor], optional
        Chain each captured chunk is cascaded through (WAV in/out, see
        :class:`~palmimo_sdk.audio.processor.AudioProcessor`) before it is
        dispatched to subscribers, in order. ``None`` (the default) means
        "echo-cancel by default": a fresh
        :class:`~palmimo_sdk.audio.aec.EchoCanceller` is built at
        :meth:`open` time — one instance per open generation, matching the
        per-generation ``stream``/``stop`` discipline below. Pass
        ``processors=[]`` explicitly for raw, unprocessed audio. A processor
        that raises is logged and its chunk is discarded (not dispatched
        unprocessed); a processor whose output is empty simply means nothing
        is dispatched for that read. Only the first processor in the chain
        may declare ``capture_channels > 1`` (see
        :class:`~palmimo_sdk.audio.processor.AudioProcessor`'s docstring) —
        :meth:`open` validates this and raises otherwise.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        blocksize: int = BLOCKSIZE,
        device_name_hint: str | None = None,
        device_key: str = "default",
        input_stream_factory: InputStreamFactory | None = None,
        processors: Sequence[AudioProcessor] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.device_name_hint = device_name_hint
        self.device_key = device_key
        self._input_stream_factory = input_stream_factory
        self._processors_arg = processors
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Guards _subs: subscribe()/unsubscribe() (any thread) vs. the capture
        # thread's fan-out, same discipline as HeadCamera's _consumers_lock.
        self._subs_lock = threading.Lock()
        self._subs: list[Subscription] = []
        self._opened = False
        # Serializes open()/close() against each other (same discipline as
        # HeadCamera's _drain_lock): without it, a concurrent open() and
        # close() could interleave and leave _stop/_thread in a state that
        # belongs to neither call. close() joins the capture thread under
        # this lock, so a concurrent open() waits up to _JOIN_TIMEOUT_S for
        # it — accepted, see close()'s docstring.
        self._lifecycle_lock = threading.Lock()
        self._log = logging.getLogger(__name__)
        # Capture-path observability, reset at the start of each open()
        # generation (see open()) — see _run for what updates them and why
        # they matter (DTLN inference runs on the capture thread).
        self.overflows = 0
        self.blocks = 0
        self.late_blocks = 0
        self.process_seconds = 0.0
        self._max_block_share = 0.0

    @property
    def is_open(self) -> bool:
        return self._opened

    def open(self) -> None:
        """Open the mic and start the background capture thread; idempotent.

        Resolves this generation's ``processors`` chain (see the
        ``processors`` parameter) BEFORE taking ``_lifecycle_lock`` — a
        ``None`` default builds a fresh
        :class:`~palmimo_sdk.audio.aec.EchoCanceller`, which can auto-download
        a model and block for up to that call's own timeout (tens of
        seconds); holding the lifecycle lock for that whole time would block
        an unrelated :meth:`close` / :attr:`is_open` check on this instance
        for no reason. The cost: two callers racing :meth:`open` on the same
        (unopened) instance can both resolve/build a processors chain before
        either takes the lock — harmless, since only one wins the lock and
        proceeds; the loser's chain (and any ``Denoiser`` it built) is simply
        discarded, wasted work but no corrupted state.

        Serialized against :meth:`close` by ``_lifecycle_lock`` — because
        ``close()`` joins the capture thread under that same lock, a caller
        racing a slow close (join wedged up to ``_JOIN_TIMEOUT_S``) simply
        waits for it here rather than interleaving with it; accepted, since
        the alternative (opening while a stale thread might still be
        winding down) is the exact hazard this whole generation scheme
        exists to avoid. ``is_open`` is checked once before resolving
        ``processors`` (a fast path so an already-open call skips that work
        entirely) and rechecked under the lock (the only check that's
        actually race-safe), so a second concurrent call can't redundantly
        register/build a second generation just because it read ``is_open``
        as ``False`` before the first call finished.

        Refuses to start a second generation while a previous capture thread
        is still alive (e.g. a prior :meth:`close` timed out and leaked the
        handle): that thread still closes over the OLD ``stream``/``stop``
        pair (and owns closing it — see :meth:`_run`) and must never be
        mistaken for this call's new generation.

        Fail-fast, in order: (1) resolve ``processors`` (above, outside the
        lock); (2) register *device_key* — raises immediately if another open
        :class:`MicStream` already holds it; (3) resolve the device and
        construct + ``start()`` the ``InputStream`` on THIS (calling) thread,
        so a bad device / permission failure raises synchronously from
        ``open()`` rather than silently failing on the background thread; (4)
        start the daemon capture thread, bound to this generation's own
        ``stream``/``stop``/``processors`` (never ``self._stop`` — see
        :meth:`_run`). Any failure in (2)-(4) unwinds everything done so far
        (unregister *device_key*; best-effort ``stop()``/``close()`` the
        stream if one was built) before re-raising as :class:`RuntimeError`,
        so a failed open never leaves a phantom registry entry or a
        half-started generation behind. ``self._stop`` / ``self._thread`` /
        ``self._opened`` are only assigned once every step above has
        succeeded.
        """
        if self._opened:
            return
        try:
            processors = self._resolve_processors()
            capture_channels = self._capture_channels(processors)
        except Exception as exc:
            raise RuntimeError(f"Cannot open mic stream (device_key={self.device_key!r}): {exc}") from exc
        required_by: str | None = None
        if processors:
            required_by = type(processors[0]).__name__
            if self._processors_arg is None:
                required_by = f"{required_by}, the default processor"
        with self._lifecycle_lock:
            if self._opened:
                return
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError(
                    f"Cannot open mic stream (device_key={self.device_key!r}): a previous "
                    "capture thread is still running (its close() likely timed out) and "
                    "still owns the old stream"
                )
            _mic_registry.register(self.device_key, self)
            stream: Any | None = None
            try:
                stream = self._build_stream(capture_channels, required_by)
                stream.start()
                stop = threading.Event()
                thread = threading.Thread(
                    target=self._run,
                    args=(stream, stop, processors, capture_channels),
                    name="palmimo-micstream",
                    daemon=True,
                )
                thread.start()
            except Exception as exc:
                _mic_registry.unregister(self.device_key, self)
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.stop()
                    with contextlib.suppress(Exception):
                        stream.close()
                raise RuntimeError(f"Cannot open mic stream (device_key={self.device_key!r}): {exc}") from exc
            self._stop = stop
            self._thread = thread
            self._opened = True
            # Reset per-generation observability counters (see _run) so a
            # reopen starts fresh rather than accumulating across generations.
            self.overflows = 0
            self.blocks = 0
            self.late_blocks = 0
            self.process_seconds = 0.0
            self._max_block_share = 0.0

    def _resolve_processors(self) -> Sequence[AudioProcessor]:
        """Resolve this open generation's processor chain.

        ``processors=None`` (the constructor default) means "echo-cancel by
        default": a fresh :class:`~palmimo_sdk.audio.aec.EchoCanceller` is
        built here — lazily imported so ``import palmimo_sdk`` / ``import
        palmimo_sdk.io`` stay dependency-free without the ``voice`` extra, and
        freshly constructed every :meth:`open` (a stateful processor belongs
        to one generation, same as ``stream``/``stop``). An explicit
        ``processors=[]`` (or any other sequence) is returned as given —
        ``[]`` is how a caller opts into raw, unprocessed audio.

        Every resolved processor that exposes a ``sample_rate`` attribute
        (see :class:`~palmimo_sdk.audio.processor.AudioProcessor`'s docstring
        for the convention) must agree with this stream's own
        :attr:`sample_rate`, checked here so a mismatch fails this call
        instead of raising on every chunk on the background thread (silently
        discarding all audio — see :meth:`_apply_processors`).

        Raises:
            RuntimeError: a resolved processor's ``sample_rate`` does not
                match this stream's ``sample_rate``.
        """
        processors = self._processors_arg
        if processors is None:
            from ..audio.aec import EchoCanceller  # lazy: keeps `import palmimo_sdk` dependency-free

            processors = (EchoCanceller(),)
        for processor in processors:
            processor_rate = getattr(processor, "sample_rate", None)
            if processor_rate is not None and processor_rate != self.sample_rate:
                raise RuntimeError(
                    f"{processor!r} is fixed at {processor_rate} Hz but this MicStream is configured for "
                    f"{self.sample_rate} Hz; pass processors=[] for raw audio or a processor built for "
                    f"{self.sample_rate} Hz"
                )
        return processors

    def _capture_channels(self, processors: Sequence[AudioProcessor]) -> int:
        """Resolve the device channel count this ``processors`` chain requires.

        Only the first processor in the chain may declare a
        ``capture_channels`` greater than 1 (see
        :class:`~palmimo_sdk.audio.processor.AudioProcessor`'s docstring for
        the convention) — every later processor consumes the previous one's
        mono output, so a later processor requiring more than one channel
        could never actually receive it.

        Raises:
            RuntimeError: A processor after the first declares
                ``capture_channels > 1``, or the first processor declares a
                ``capture_channels`` that isn't a positive ``int``.
        """
        if not processors:
            return 1
        for processor in processors[1:]:
            channels = getattr(processor, "capture_channels", 1)
            if channels > 1:
                raise RuntimeError(
                    f"{processor!r} declares capture_channels={channels}, but only the first "
                    "processor in a processors chain may require more than 1 channel — every "
                    "later processor receives the previous one's mono output"
                )
        first_channels = getattr(processors[0], "capture_channels", 1)
        if not (isinstance(first_channels, int) and first_channels >= 1):
            raise RuntimeError(
                f"{processors[0]!r} declares capture_channels={first_channels!r}, which is not a "
                "positive int; a mic stream cannot be opened with an invalid channel count"
            )
        return first_channels

    def _build_stream(self, capture_channels: int, required_by: str | None = None) -> Any:
        factory = self._input_stream_factory
        if factory is None:
            import sounddevice as sd  # lazy: keeps `import palmimo_sdk` dependency-free

            device = _resolve_device(
                sd, self.device_name_hint, self._log, min_channels=capture_channels, required_by=required_by
            )
            return sd.InputStream(
                samplerate=self.sample_rate,
                channels=capture_channels,
                dtype="int16",
                blocksize=self.blocksize,
                device=device,
            )
        return factory(self.sample_rate, capture_channels, "int16", self.blocksize, self.device_name_hint)

    def close(self) -> None:
        """Signal the capture thread to stop and wait (bounded) for it; idempotent.

        Runs under ``_lifecycle_lock`` (serialized against :meth:`open` — see
        there). Order: signal stop -> join the capture thread (bounded,
        ``_JOIN_TIMEOUT_S``) -> unregister *device_key*.

        Unlike an earlier version of this method, ``close()`` never touches
        the ``stream`` itself — ownership of stopping/closing it belongs
        entirely to the capture thread that opened it (see :meth:`_run`'s
        ``finally``), the same "a generation's thread owns its own
        stream/stop pair, start to finish" model an earlier internal
        prototype's ``with stream:`` used. That means a join that times out here (thread
        wedged in a blocking ``stream.read``, e.g. a yanked USB mic) no
        longer leaves the device handle to be silently forgotten forever:
        whenever that thread's ``read`` eventually returns or raises — even
        long after this call gave up waiting — its own ``finally`` still
        stops/closes the stream before exiting. This call just can't wait
        around for that (unbounded), so it logs a warning and proactively
        notifies subscribers itself instead (see :meth:`_notify_closed`) —
        nobody is left blocked on ``Subscription.get()`` / iteration just
        because the underlying thread is stuck. A subscriber can therefore
        see the sentinel twice (once from here, once later from the thread's
        own ``finally``); harmless — iteration ends at the first one and the
        second is just discarded. *device_key* is still unregistered and
        :attr:`is_open` still goes ``False`` either way, so a caller can't
        tell the difference from the public API — only the warning log marks
        the leak (bounded in time, not permanent).
        """
        with self._lifecycle_lock:
            if not self._opened:
                return
            self._opened = False
            self._stop.set()
            thread = self._thread
            if thread is not None:
                thread.join(timeout=_JOIN_TIMEOUT_S)
                if not thread.is_alive():
                    self._thread = None
                else:
                    self._log.warning(
                        "MicStream.close: capture thread for device_key=%r did not stop within "
                        "%.1fs; it still owns the stream and will release it itself once its "
                        "current read() returns",
                        self.device_key,
                        _JOIN_TIMEOUT_S,
                    )
                    # The wedged thread's own finally will eventually send the
                    # sentinel too (or never, if it's truly stuck forever) —
                    # don't make a blocked subscriber wait on that; deliver it
                    # now.
                    self._notify_closed()
            _mic_registry.unregister(self.device_key, self)

    def __enter__(self) -> MicStream:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Consumers
    # ------------------------------------------------------------------
    def subscribe(self, maxsize: int = _DEFAULT_SUB_MAXSIZE) -> Subscription:
        """Register a new :class:`Subscription` that receives every future chunk.

        Safe to call before :meth:`open` (the subscription just waits) or
        while the capture thread is running. A stream that has closed and
        reopened starts its new generation with zero subscribers — closing
        clears every prior subscription (see :meth:`_notify_closed`), so
        anyone who wants chunks again after a reopen must call
        :meth:`subscribe` / :meth:`stream` again; a subscription from before
        the close will not start receiving the new generation's audio.
        """
        sub = Subscription(self, maxsize=maxsize)
        with self._subs_lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        """Remove *sub* from the fan-out list. Idempotent."""
        with self._subs_lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def stream(self, maxsize: int = _DEFAULT_SUB_MAXSIZE) -> Subscription:
        """Sugar for ``self.subscribe(maxsize)`` — yields chunks until :meth:`close`.

        Returns the :class:`Subscription` itself (it is already an
        ``Iterator``, so an existing ``for chunk in stream.stream():`` loop
        is unaffected) rather than a plain iterator, so a caller who wants to
        unsubscribe early can. The typical form uses it as a context manager,
        which unsubscribes on the way out even if the loop body raises::

            with mic.stream() as chunks:
                for chunk in chunks:
                    ...

        Not exiting the ``with`` (or otherwise calling
        :meth:`~Subscription.close`) leaves the subscription registered — the
        capture thread keeps writing to (and eventually drop-oldest-ing into)
        its queue for as long as the stream stays open, wasted work for a
        subscriber nobody is draining anymore.

        Note this is a different lifecycle layer than ``with MicStream() as
        mic:`` (which opens/closes the *device*) — this call only manages one
        subscription against an already-open (or not-yet-open) stream.
        """
        return self.subscribe(maxsize=maxsize)

    def record(self, seconds: float) -> bytes | None:
        """Capture ``seconds`` of audio via a temporary subscription; return WAV bytes.

        The audio collected is whatever this stream's ``processors`` chain
        produced (see :class:`MicStream`'s ``processors`` parameter) — already
        echo-cancelled by default, raw only if this stream was opened with
        ``processors=[]``.

        Raises :class:`ValueError` if ``seconds`` is not positive — a caller
        bug, distinct from a runtime capture failure. Runtime failure instead
        returns ``None`` (mirrors :meth:`Microphone.record`'s failure
        semantics): if the stream isn't open, closes mid-capture, or no chunk
        arrives for ``seconds + _RECORD_TIMEOUT_MARGIN_S`` — e.g. the capture
        thread died or the device stopped delivering.
        """
        if seconds <= 0:
            raise ValueError("seconds must be positive")
        if not self._opened:
            return None
        sub = self.subscribe()
        try:
            return self._collect_wav(sub, seconds)
        finally:
            self.unsubscribe(sub)

    def _collect_wav(self, sub: Subscription, seconds: float) -> bytes | None:
        import numpy as np  # lazy: keeps `import palmimo_sdk` dependency-free

        target_samples = round(self.sample_rate * seconds)
        collected: list[Any] = []
        total = 0
        deadline = time.monotonic() + seconds + _RECORD_TIMEOUT_MARGIN_S
        while total < target_samples:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            chunk = sub.get(timeout=remaining)
            if chunk is None:  # stream closed, or this get() itself timed out
                return None
            collected.append(chunk)
            total += len(chunk)
        data = np.concatenate(collected)[:target_samples].astype(np.int16)
        return int16_to_wav(data, self.sample_rate)

    # ------------------------------------------------------------------
    # Capture thread
    # ------------------------------------------------------------------
    def _run(
        self, stream: Any, stop: threading.Event, processors: Sequence[AudioProcessor], capture_channels: int
    ) -> None:
        """Background thread body: read continuously, process, fan out, backoff on failure.

        Takes *stream*, *stop*, *processors*, and *capture_channels* as
        arguments — this generation's own set, closed over by the
        ``threading.Thread`` that calls this method — rather than reading
        ``self._stop``. That attribute gets reassigned to a NEW generation's
        object on the next :meth:`open`, and a thread whose :meth:`close`
        join timed out (still alive, still running this loop) must keep
        operating on the OLD stop/processors/capture_channels it started
        with, never get silently redirected onto whatever the next
        generation installs on ``self``. Same reasoning as
        :class:`~palmimo_sdk.io.camera.HeadCamera`'s per-generation drain stop
        event.

        This thread OWNS *stream* for its entire lifetime, start to finish:
        it is the only thing that ever calls ``stream.stop()`` /
        ``stream.close()``, unconditionally in the ``finally`` below, no
        matter how the loop above exits (a clean stop, an unhandled
        exception, ...) — the same guarantee a ``with stream:`` block would
        give. :meth:`close` never touches *stream* itself (see its
        docstring): even a thread that stays wedged in a blocking
        ``stream.read`` well past :meth:`close`'s join timeout still releases
        the device itself the moment that ``read`` finally returns or raises,
        instead of leaking the handle for the rest of the process's life.

        A read failure (USB hiccup, device pulled) must not kill the loop, but retrying with no
        wait at all would spin at 100% CPU if the device stays gone — so
        failures back off (capped) via ``stop.wait``, which still wakes
        immediately on :meth:`close`.

        Each successfully read chunk is cascaded through *processors* (see
        :meth:`_apply_processors`) before being dispatched. A processor
        exception discards that chunk (logged, not dispatched raw); an empty
        processed result (buffering, e.g. a streaming denoiser) simply means
        nothing is dispatched for that read — neither counts as a read
        failure, so ``consecutive_failures`` / backoff are untouched.

        Each read's overflow flag and each :meth:`_apply_processors` call's
        wall-clock time are tracked (``overflows``/``blocks``/``late_blocks``/
        ``process_seconds``, reset per generation in :meth:`open`) regardless
        of whether *processors* is empty — an empty chain still has
        meaningful overflow/timing info, it's just cheap to gather. This
        matters because a processor (e.g. DTLN-AEC) runs ON this thread, so a
        host too slow to keep up (a Raspberry Pi under load) would otherwise
        drop samples with no visibility at all. See the summary log in the
        ``finally`` below.
        """
        consecutive_failures = 0
        try:
            while not stop.is_set():
                try:
                    data, overflowed = stream.read(self.blocksize)
                except Exception as exc:
                    consecutive_failures += 1
                    backoff = min(0.05 * consecutive_failures, 1.0)
                    self._log.warning(
                        "MicStream read failed (%d in a row), backing off %.2fs: %s",
                        consecutive_failures,
                        backoff,
                        exc,
                    )
                    stop.wait(backoff)
                    continue
                consecutive_failures = 0
                if overflowed:
                    self.overflows += 1
                    self._log.warning(
                        "MicStream input overflow on device_key=%r (%d total this generation): "
                        "the capture thread is not keeping up with the device",
                        self.device_key,
                        self.overflows,
                    )
                # (frames, capture_channels) int16. A single-channel device
                # collapses to 1-D mono (the common case); a multi-channel
                # capture (e.g. an EchoCanceller's near+reference channels)
                # keeps the full block — the first processor is the one that
                # knows how to split it. read() returns a fresh array each
                # call, so no extra copy is needed here.
                chunk = data if capture_channels > 1 else data[:, 0]
                start = time.perf_counter()
                processed = self._apply_processors(chunk, processors, capture_channels)
                elapsed = time.perf_counter() - start
                self.blocks += 1
                self.process_seconds += elapsed
                budget = len(data) / self.sample_rate
                if budget > 0:
                    self._max_block_share = max(self._max_block_share, elapsed / budget)
                    if elapsed > LATE_BLOCK_FRACTION * budget:
                        self.late_blocks += 1
                if processed is not None and len(processed) > 0:
                    self._dispatch(processed)
        finally:
            # This generation's thread owns stream — see the docstring above.
            # close() never touches it, including on a join timeout, so this
            # is the only place it's ever released, however _run exits.
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()
            if self.blocks > 0:
                mean_share = self.process_seconds / (self.blocks * self.blocksize / self.sample_rate)
                self._log.info(
                    "MicStream capture (device_key=%r): %d blocks, %.0f%% of real time spent "
                    "processing (max %.0f%%), %d late, %d overflow(s)",
                    self.device_key,
                    self.blocks,
                    mean_share * 100.0,
                    self._max_block_share * 100.0,
                    self.late_blocks,
                    self.overflows,
                )
            self._notify_closed()

    def _apply_processors(self, chunk: Any, processors: Sequence[AudioProcessor], capture_channels: int) -> Any | None:
        """Cascade *chunk* through *processors*; return int16 samples, or ``None`` on failure.

        Empty *processors* returns *chunk* unchanged, skipping the WAV
        round-trip entirely (the common raw-audio path) — *capture_channels*
        is always 1 in that case (see :meth:`_capture_channels`), so *chunk*
        is already mono. Otherwise *chunk* is encoded to WAV once (with
        *capture_channels* interleaved channels) and each processor's
        :meth:`~palmimo_sdk.audio.processor.AudioProcessor.process` receives
        the previous one's WAV output in order, so the effect cascades (e.g.
        echo-cancel -> a second transform). Every processor's output is mono
        per the :class:`~palmimo_sdk.audio.processor.AudioProcessor` contract,
        so the final decode below is always mono regardless of
        *capture_channels*. Any exception raised by a processor is logged and
        ``None`` is returned — the caller discards that chunk instead of
        dispatching unprocessed (possibly noisy) audio silently.
        """
        if not processors:
            return chunk
        try:
            wav = int16_to_wav(chunk, self.sample_rate, channels=capture_channels)
            for processor in processors:
                wav = processor.process(wav)
            samples, _sample_rate = wav_to_int16(wav)
            return samples
        except Exception as exc:
            self._log.warning("MicStream processor raised, discarding chunk: %s", exc)
            return None

    def _dispatch(self, chunk: Any) -> None:
        # Read-only before fan-out: the same chunk object is handed to every
        # subscriber, so one subscriber mutating it in place would corrupt
        # what the others see (raw chunks are a writable view into the
        # stream's read buffer; processed chunks already happen to be
        # read-only via frombuffer, but pin that as a guarantee here rather
        # than an accident of one path's implementation — see Subscription's
        # docstring).
        chunk.setflags(write=False)
        with self._subs_lock:
            subs = list(self._subs)
        for sub in subs:
            self._enqueue(sub, chunk, count_drop=True)

    def _notify_closed(self) -> None:
        """Deliver the close sentinel to every current subscriber, then forget them.

        Clearing :attr:`_subs` after fan-out (rather than leaving stale
        entries around) is what makes :meth:`subscribe`'s "a reopened stream
        starts with zero subscribers" guarantee true: without this, a
        subscription made before :meth:`close` would sit in the list forever
        and start silently receiving the NEXT generation's audio the moment a
        later :meth:`open` starts dispatching again — a stale consumer
        listening in on a stream it never subscribed to this time around.
        Safe to call twice (see :meth:`close`'s docstring on the sentinel
        potentially being sent from both there and here): the second call
        just finds :attr:`_subs` already empty.
        """
        with self._subs_lock:
            subs = list(self._subs)
            self._subs.clear()
        for sub in subs:
            self._enqueue(sub, _CLOSE_SENTINEL, count_drop=False)

    @staticmethod
    def _enqueue(sub: Subscription, item: Any, *, count_drop: bool) -> None:
        """Put *item* on *sub*'s queue, dropping the oldest entry if it's full.

        Used for both real chunks (where a drop is counted in
        :attr:`Subscription.dropped` — a slow consumer losing audio should be
        observable) and the close sentinel (where delivery must never be
        skipped just because the consumer fell behind, so the same
        make-room-and-retry happens but isn't counted as an audio drop).

        For a real chunk (*count_drop* ``True``), one make-room-and-retry is
        enough: if the second ``put_nowait`` also loses (another producer
        refilled the queue in between), that just means yet another slot was
        available for something newer, business as usual for drop-oldest.
        The close sentinel (*count_drop* ``False``) gets no such pass:
        losing it would leave a consumer that had drained the queue blocked
        forever, waiting for a stream that already closed. So delivery is
        retried — drain one more entry and reattempt — until it lands or the
        queue's own capacity is exhausted (bounded: at most ``maxsize + 1``
        attempts, since nothing can refill faster than this loop drains), and
        if even that somehow doesn't land, one last bounded blocking
        ``put`` is tried (suppressing failure) rather than looping forever.
        """
        try:
            sub._queue.put_nowait(item)
            return
        except queue.Full:
            pass
        if count_drop:
            with contextlib.suppress(queue.Empty):
                sub._queue.get_nowait()
                sub.dropped += 1
            with contextlib.suppress(queue.Full):
                sub._queue.put_nowait(item)
            return
        attempts = (sub._queue.maxsize or _DEFAULT_SUB_MAXSIZE) + 1
        for _ in range(attempts):
            with contextlib.suppress(queue.Empty):
                sub._queue.get_nowait()
            try:
                sub._queue.put_nowait(item)
                return
            except queue.Full:
                continue
        with contextlib.suppress(Exception):
            sub._queue.put(item, timeout=0.5)


def _resolve_device(
    sd: Any,
    hint: str | None,
    log: logging.Logger,
    *,
    min_channels: int = 1,
    required_by: str | None = None,
) -> int | None:
    """Resolve *hint* to a sounddevice input device index via ``sd.query_devices()``.

    ReSpeaker-style USB mics enumerate at a mount-dependent ALSA card index, so a fixed index
    is unusable — a case-insensitive substring match on the device name is.
    ``None`` (no hint, no match, or the query itself fails) means "use
    sounddevice's default input device".

    *min_channels* additionally requires a candidate device to expose at
    least that many input channels — a hint match alone isn't enough when the
    first processor in the chain (e.g. a 6-channel
    :class:`~palmimo_sdk.audio.aec.EchoCanceller`) needs more than the device
    it happens to match actually has.

    When *min_channels* is greater than 1 and no device satisfies both the
    hint and the channel requirement, this raises instead of silently falling
    back to the default input — a fallback would only fail later, deep inside
    PortAudio, with a far less actionable error. The *min_channels* <= 1 path
    (today's common case) is unchanged: log a warning and return ``None``.

    Args:
        sd: The ``sounddevice`` module (or a stand-in exposing
            ``query_devices()``).
        hint: Case-insensitive substring match against device names; falsy
            means "use the default input device".
        log: Logger for the warning / info messages this function emits.
        min_channels: Minimum ``max_input_channels`` a candidate device must
            report.
        required_by: Human-readable label for the caller (e.g. the first
            processor's class name) — folded into the error message so a
            failure is actionable.

    Raises:
        RuntimeError: *min_channels* is greater than 1 and no device
            satisfies both the hint and the channel requirement.
    """
    hint_low = None if not hint else hint.lower()
    try:
        devices = sd.query_devices()
    except Exception as exc:
        log.warning("sounddevice.query_devices() failed, using the default input: %s", exc)
        return None
    if hint_low is None and min_channels <= 1:
        return None
    for idx, dev in enumerate(devices):
        name = str(dev.get("name", ""))
        if hint_low is not None and hint_low not in name.lower():
            continue
        if dev.get("max_input_channels", 0) < max(min_channels, 1):
            continue
        log.info("Selected input device [%d] %s", idx, name)
        return idx
    if min_channels > 1:
        remedy = "pass processors=[Denoiser()] for noise-only or processors=[] for raw capture"
        suffix = f" (required by {required_by})" if required_by else ""
        raise RuntimeError(f"no input device with >= {min_channels} channels{suffix}; {remedy}")
    if hint:
        log.warning("No input device matches hint %r; using the default input", hint)
    return None
