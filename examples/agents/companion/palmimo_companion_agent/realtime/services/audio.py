"""Microphone in, spoken audio out: the two directions of the Realtime call.

:class:`MicrophoneFeed` reads capture chunks and appends them to the API's
input buffer; :class:`Playback` plays the model's own audio through
``aplay`` and can be killed mid-sentence for a barge-in; :class:`PitchShifter`
raises the played-back voice to suit a small creature.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import queue
import subprocess
import sys
import threading
from fractions import Fraction
from typing import Any, Protocol

import numpy as np
from scipy.signal import resample_poly

from ..client import RealtimeClientLike
from ..protocol import AudioAppend


#: The capture pipeline is pinned to 16 kHz by the canceller and the voice
#: detector upstream; the Realtime API speaks 24 kHz. Every chunk is
#: resampled 3/2 on the way out.
CAPTURE_RATE = 16000
API_RATE = 24000

#: MicrophoneFeed reads with a bounded blocking call rather than an unbounded
#: one so cancellation is noticed promptly: the executor thread reading
#: chunks.get() cannot itself be cancelled, so without a timeout it would sit
#: blocked on the queue for up to the rest of the session after this
#: service's own task is cancelled -- an orphaned thread, not a leaked one
#: (it still exits once the stream closes), but one that outlives the
#: service unnecessarily. This is NOT a stop-flag poll -- see services/base.py.
_MIC_READ_TIMEOUT_S = 0.2


class MicStreamLike(Protocol):
    """The subset of :class:`~palmimo_sdk.io.mic_stream.MicStream` this service needs."""

    def stream(self) -> Any: ...


class MicrophoneFeed:
    """Reads capture chunks, resamples 16k -> 24k, and appends them to the API's input buffer."""

    name = "microphone"

    def __init__(self, mic: MicStreamLike, client: RealtimeClientLike) -> None:
        self._mic = mic
        self._client = client

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        with self._mic.stream() as chunks:
            while True:
                chunk = await loop.run_in_executor(None, chunks.get, _MIC_READ_TIMEOUT_S)
                if chunk is None:
                    continue
                upsampled = resample_poly(np.asarray(chunk, dtype=np.float32), 3, 2)
                pcm = np.clip(upsampled, -32768, 32767).astype(np.int16).tobytes()
                await self._client.send(AudioAppend(audio_b64=base64.b64encode(pcm).decode()))


class PitchShifter:
    """Raises the voice by resampling each chunk on its way to the speaker.

    Fewer samples out at an unchanged rate means a higher pitch and slightly
    quicker speech. Constant-tempo pitch shifting would need a phase vocoder,
    costing lookahead and CPU this machine does not have spare.

    The ratio uses a small denominator because ``resample_poly``'s filter
    length grows with it, and a long filter on a short chunk smears the edges.
    """

    def __init__(self, pitch: float) -> None:
        ratio = Fraction(1.0 / pitch).limit_denominator(64)
        self.up, self.down = ratio.numerator, ratio.denominator

    def process(self, pcm: bytes) -> bytes:
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if samples.size == 0:
            return pcm
        shifted = resample_poly(samples, self.up, self.down)
        return np.clip(shifted, -32768, 32767).astype(np.int16).tobytes()


def _kill_and_reap(process: subprocess.Popen[bytes]) -> None:
    """:meth:`Playback.interrupt`'s off-loop teardown: kill, close stdin, bounded reap.

    Run on a dedicated thread by :meth:`Playback.interrupt`, never on the
    caller's own thread -- see that method's docstring for why. ``kill()``
    runs before ``stdin.close()`` for the same reason :meth:`Playback.close`
    orders them that way (see its docstring): a writer thread wedged inside
    a blocking ``stdin.write()`` holds that stdin object's own internal
    lock, so closing it first could block this thread indefinitely; killing
    first makes the wedged write fail (EPIPE) and release that lock.
    ``kill()`` only sends the signal; without reaping, the process stays a
    zombie until something waits on it. The reap is bounded so an
    already-dead/unresponsive process cannot leave this background thread
    stuck forever (harmless on its own since it is never joined by this
    function -- see :meth:`Playback.close` for how it is still supervised --
    but pointless to let run past the process actually being gone).
    """
    with contextlib.suppress(Exception):
        process.kill()
    with contextlib.suppress(Exception):
        if process.stdin is not None:
            process.stdin.close()
    with contextlib.suppress(Exception):
        process.wait(timeout=1)


class Playback:
    """A raw-PCM ``aplay`` pipe that can be dropped mid-sentence, fed off a writer thread.

    ``write()`` only enqueues -- the actual ``stdin.write()``/``flush()`` (and
    the pitch-shift CPU work) happen on a dedicated writer thread, because
    ALSA backpressure on a blocking write would otherwise stall the caller,
    which in practice is the event loop's own :class:`~..services.router.EventRouter`
    -- exactly the moment barge-in needs the loop free to react. Interrupting
    means killing the process: ALSA has already buffered what was written to
    it, so asking it to stop at the end of the current chunk would not stop
    it in time to be barge-in. The kill/reap is itself dispatched to a
    throwaway thread (see :func:`_kill_and_reap`) for the same reason --
    :meth:`interrupt` is called synchronously from the event loop, and a
    blocking ``process.wait()`` there would reintroduce the exact stall this
    class exists to avoid.

    ``write()``, :meth:`interrupt`, and :meth:`close` are only ever meant to
    be called from a single thread -- the event loop's, via
    :class:`~..services.router.EventRouter` / :class:`~..services.router.BargeIn`
    -- never from each other and never concurrently with themselves. That
    serialization is load-bearing: it is what lets ``close()`` discard the
    queue and enqueue its sentinel without racing a concurrent ``write()``,
    and what lets ``interrupt()`` bump the generation counter and clear the
    queue as one atomic-from-the-caller's-view step. The writer thread is the
    only other thread that touches this instance's internals, and only the
    parts documented as such below.

    ``device`` is a resolved ALSA device string (``plughw:CARD=...``) for
    ``aplay -D``; ``None`` spawns ``aplay`` without one and takes ALSA's
    default. It is resolved by the caller rather than here because this class
    is constructed once a session is already up, by which point the device
    listing is settled -- unlike :class:`~palmimo_sdk.io.speaker.Speaker`,
    which is built before the USB device is necessarily attached and so has to
    resolve lazily.
    """

    #: How many consecutive failed writes must pass before the writer warns
    #: again. The first failure always warns immediately; after that, staying
    #: quiet for a while avoids flooding stderr with one line per chunk while
    #: still leaving a trail if the condition never clears.
    _WARN_EVERY = 50

    def __init__(self, pitch: PitchShifter | None = None, device: str | None = None) -> None:
        self._pitch = pitch
        self._device = device
        self._process: subprocess.Popen[bytes] | None = None
        self._process_lock = threading.Lock()
        #: Guarded by ``_process_lock`` alongside ``_process``: once set,
        #: `_ensure_process` must never spawn a new ``aplay`` again, even if
        #: the writer thread is still draining a straggler chunk.
        self._closed = False
        #: Bumped by `interrupt`, guarded by ``_process_lock``. The writer
        #: snapshots this value right after dequeuing a chunk;
        #: `_ensure_process` refuses to spawn or reuse a process for a
        #: stale generation, so a chunk that was already in flight when an
        #: interrupt fired -- pre-barge-in audio -- cannot start (or write
        #: to) a fresh ``aplay`` after the fact.
        self._generation = 0
        #: (thread, process) pairs for kill/reap work `interrupt` handed off
        #: to a background thread that has not finished yet -- guarded by
        #: ``_process_lock``, self-pruning as each thread completes (see
        #: `_kill_and_reap_tracked`). Lets `close` supervise a process
        #: `interrupt` is still tearing down instead of treating
        #: ``_process is None`` as nothing left to reap.
        self._pending_kills: list[tuple[threading.Thread, subprocess.Popen[bytes]]] = []
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._writer: threading.Thread | None = None
        #: Consecutive failed writes, touched only on the writer thread.
        self._write_failures = 0

    def _ensure_writer(self) -> None:
        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(target=self._drain, name="playback-writer", daemon=True)
            self._writer.start()

    def _ensure_process(self, generation: int) -> subprocess.Popen[bytes] | None:
        """Return the live ``aplay`` process for *generation*, starting one if needed -- or ``None`` if stale/closed.

        *generation* is the interrupt generation the writer captured right
        after dequeuing the chunk this call is for. Returns ``None`` --
        instead of spawning or handing back a process to write to -- once
        :meth:`close` has run, or once :meth:`interrupt` has moved the
        session past the generation this chunk belongs to: an interrupt
        that fires while a chunk is between the queue and the pipe must
        still be able to drop it, not let it start (or write to) a process
        the interrupt already considers gone.
        """
        with self._process_lock:
            if self._closed or generation != self._generation:
                return None
            if self._process is None or self._process.poll() is not None:
                device = ["-D", self._device] if self._device else []
                self._process = subprocess.Popen(
                    ["aplay", "-q", *device, "-t", "raw", "-f", "S16_LE", "-r", str(API_RATE), "-c", "1"],
                    stdin=subprocess.PIPE,
                )
            return self._process

    def _record_write_failure(self) -> None:
        """Count a playback failure and warn the operator on the first one and every ``_WARN_EVERY``-th after.

        Only called for failures not explained by an in-flight
        :meth:`interrupt` -- barge-in killing the process mid-write is
        expected and must not look like a dead audio device (see
        :meth:`_drain`'s ``stale`` check).
        """
        self._write_failures += 1
        if self._write_failures == 1 or self._write_failures % self._WARN_EVERY == 0:
            print(
                f"!! playback write failed ({self._write_failures} in a row) -- "
                "check the ALSA default output device and whether it is held "
                "by another process; the robot may be silent",
                file=sys.stderr,
                flush=True,
            )

    def _drain(self) -> None:
        """Writer-thread body: pitch-shift and write queued chunks until the ``close()`` sentinel.

        Outlives any one ``aplay`` process: :meth:`interrupt` may kill the
        current one mid-session, and the next queued chunk simply starts a
        new one via :meth:`_ensure_process` -- unless :meth:`close` has
        already run, or :meth:`interrupt` has moved on since this chunk was
        dequeued (see the ``generation`` snapshot below), in which case the
        chunk is dropped instead.

        Pitch processing runs inside the same failure handling as the write
        itself: a malformed chunk is recorded like any other playback
        failure rather than crashing this thread. An uncaught exception here
        would kill the thread silently -- the next ``write()`` call would
        just start a fresh one via :meth:`_ensure_writer`, with no trace of
        what happened to the old one.
        """
        while True:
            pcm = self._queue.get()
            if pcm is None:  # sentinel: close() is stopping this thread for good
                return
            with self._process_lock:
                generation = self._generation
            try:
                if self._pitch is not None:
                    pcm = self._pitch.process(pcm)
            except Exception:
                self._record_write_failure()
                continue
            process = self._ensure_process(generation)
            if process is None or process.stdin is None:
                continue
            try:
                process.stdin.write(pcm)
                process.stdin.flush()
            except Exception:
                with self._process_lock:
                    stale = process is not self._process or generation != self._generation
                if not stale:
                    self._record_write_failure()
                # else: interrupt() already reclaimed this chunk's process --
                # an expected teardown, not a playback failure worth a warning.
            else:
                self._write_failures = 0

    def write(self, pcm: bytes) -> None:
        """Enqueue *pcm* for the writer thread. Never blocks -- safe to call from the event loop.

        A no-op once :meth:`close` has run: handing a chunk to a stopped
        writer would either resurrect a writer thread that will never be
        told to exit again, or queue behind a sentinel that already made the
        writer exit -- either way the chunk would never be dropped cleanly.
        See the class docstring for why this check does not need its own
        lock around the queue.
        """
        with self._process_lock:
            if self._closed:
                return
        self._ensure_writer()
        self._queue.put_nowait(pcm)

    def _kill_and_reap_tracked(self, process: subprocess.Popen[bytes]) -> None:
        """:meth:`interrupt`'s off-loop teardown, self-pruning from ``_pending_kills`` when done."""
        try:
            _kill_and_reap(process)
        finally:
            with self._process_lock:
                self._pending_kills = [
                    (thread, proc) for thread, proc in self._pending_kills if thread is not threading.current_thread()
                ]

    def interrupt(self) -> None:
        """Drop whatever is queued and kill playback instantly, without blocking the caller.

        Called synchronously from the event loop (:class:`~..services.router.BargeIn`),
        so the queue is cleared with non-blocking gets and the actual kill/reap
        is handed to :func:`_kill_and_reap` on its own thread -- see the class
        docstring. Bumping ``_generation`` here, under the same lock that
        clears ``_process``, is what makes a chunk the writer had already
        dequeued before this call -- pre-barge-in audio still in flight
        between the queue and the pipe -- get dropped instead of starting
        (or writing to) a fresh ``aplay`` afterwards: see
        :meth:`_ensure_process` and :meth:`_drain`. The kill thread is
        tracked in ``_pending_kills`` so :meth:`close`, if it runs right
        after, can still supervise it instead of treating ``_process is
        None`` as "nothing left to reap."
        """
        with contextlib.suppress(queue.Empty):
            while True:
                self._queue.get_nowait()
        with self._process_lock:
            process, self._process = self._process, None
            self._generation += 1
        if process is None:
            return
        thread = threading.Thread(target=self._kill_and_reap_tracked, args=(process,), daemon=True)
        with self._process_lock:
            self._pending_kills.append((thread, process))
        thread.start()

    def close(self) -> None:
        """Stop the writer thread and reap the current process. Allowed to block: this runs once, at session end.

        The queue is discarded (not drained through the writer) before the
        stop sentinel is enqueued, so the writer sees the sentinel next and
        exits promptly instead of working through however much audio is
        still backed up -- by session end the motions have already been
        parked, so finishing a whole queued reply is not owed; audio already
        sitting in ``aplay``'s own buffer may still finish playing out, only
        what was queued but not yet written is dropped. ``_closed`` is set
        first so a chunk the writer had already dequeued before ``close()``
        ran is dropped by :meth:`_ensure_process` rather than spawning a
        fresh ``aplay`` that would outlive this call.

        ``process.kill()`` runs before ``process.stdin.close()``. Reversing
        that order can hang this call indefinitely: a writer thread wedged
        inside a blocking ``stdin.write()`` (ALSA backpressure -- the same
        condition that makes the writer join below time out) holds that
        stdin object's own internal lock, so closing it first blocks this
        call until the write itself returns -- which, for a genuinely
        wedged write, never happens on its own. Killing first makes the
        wedged write fail (EPIPE) and release that lock, so the
        ``stdin.close()`` that follows is no longer waiting on anything. The
        same reasoning applies to :func:`_kill_and_reap` on the
        :meth:`interrupt` path.

        Any kill/reap thread still running from a recent :meth:`interrupt`
        is joined here too (bounded): a process ``interrupt()`` handed off
        right before ``close()`` ran must not be abandoned just because
        ``_process`` was already ``None`` by the time this method looked at
        it. If that join still times out, the process is killed directly --
        the tracked entry holds the ``Popen`` itself, so this does not need
        the other thread to finish. A ``wait()`` that times out on this
        call's own process is followed by a ``kill()`` and one more bounded
        ``wait()`` rather than giving up -- so a live ``aplay`` is never
        abandoned on the way out.
        """
        with self._process_lock:
            self._closed = True
        if self._writer is not None:
            with contextlib.suppress(queue.Empty):
                while True:
                    self._queue.get_nowait()
            self._queue.put(None)  # sentinel: stop the writer thread
            self._writer.join(timeout=5)
        with self._process_lock:
            process, self._process = self._process, None
            pending = list(self._pending_kills)
        for thread, pending_process in pending:
            thread.join(timeout=2)
            if thread.is_alive():
                with contextlib.suppress(Exception):
                    pending_process.kill()
                with contextlib.suppress(Exception):
                    pending_process.wait(timeout=2)
        if process is None:
            return
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            if process.stdin is not None:
                process.stdin.close()
        try:
            process.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                process.wait(timeout=5)


__all__ = ["API_RATE", "CAPTURE_RATE", "MicStreamLike", "MicrophoneFeed", "PitchShifter", "Playback"]
