"""MicStream resource tests.

Only the hardware bit (``sounddevice.InputStream``) is faked, via the
``input_stream_factory`` seam — the real background-thread capture loop,
fan-out, drop-oldest, backoff, and WAV assembly all run for real, so the
behaviour is genuinely exercised without a mic or ``sounddevice`` installed.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import wave
from collections.abc import Callable, Iterator
from io import BytesIO
from typing import Any

import numpy as np
import pytest

from palmimo_sdk.audio.processor import int16_to_wav, wav_to_int16, wav_to_int16_multi
from palmimo_sdk.io import _mic_registry
from palmimo_sdk.io import mic_stream as mic_stream_module
from palmimo_sdk.io.mic_stream import MicStream, Subscription


# ----------------------------------------------------------------------
# Fake InputStream — mirrors sounddevice.InputStream's start/read/stop/close.
# ----------------------------------------------------------------------
class _FakeInputStream:
    """Records init args; read() drains a pre-loadable queue, else returns filler.

    Filler chunks (returned once the queue is empty) carry an incrementing
    fill value, purely so a test can tell "real" pushed chunks apart from
    filler if it ever needs to. A tiny sleep on the filler path keeps an
    idle background thread from spinning at 100% CPU during a test.
    """

    def __init__(self, samplerate: int, channels: int, dtype: str, blocksize: int, device: int | None):
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.blocksize = blocksize
        self.device = device
        self.started = False
        self.stopped = False
        self.closed = False
        self._queue: queue.Queue = queue.Queue()
        self._fill_value = 0

    def push_chunk(self, values: list[int] | list[list[int]] | np.ndarray, overflowed: bool = False) -> None:
        """Queue a chunk for the next ``read()``.

        *values* is either a flat 1-D sequence (broadcast to a single-channel
        ``(frames, 1)`` block, the common case in this file) or an already
        2-D ``(frames, channels)`` array/nested sequence, for tests that push
        a genuine multi-channel block straight through. *overflowed* is
        returned as ``read()``'s second element for this chunk — default
        ``False`` preserves every pre-existing call site in this file.
        """
        arr = np.array(values, dtype=np.int16)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        self._queue.put(("chunk", arr, overflowed))

    def push_error(self, exc: Exception) -> None:
        self._queue.put(("error", exc, False))

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def read(self, n: int) -> tuple[np.ndarray, bool]:
        try:
            kind, val, overflowed = self._queue.get_nowait()
        except queue.Empty:
            # Filler chunks approximate a real device's continuous sampling
            # (read() never truly blocks forever — hardware keeps producing
            # frames at the sample rate). Always NEGATIVE so tests can tell a
            # deliberately pushed (positive) marker chunk apart from filler
            # unambiguously, however the two races against each other.
            time.sleep(0.005)
            self._fill_value += 1
            return np.full((n, 1), -self._fill_value, dtype=np.int16), False
        if kind == "error":
            raise val
        return val, overflowed


class _RecordingFactory:
    """Callable ``input_stream_factory``: records every call's args, hands back one fixed stream."""

    def __init__(self, stream: _FakeInputStream) -> None:
        self.stream = stream
        self.calls: list[dict] = []

    def __call__(
        self, samplerate: int, channels: int, dtype: str, blocksize: int, device: int | None
    ) -> _FakeInputStream:
        self.calls.append(
            {"samplerate": samplerate, "channels": channels, "dtype": dtype, "blocksize": blocksize, "device": device}
        )
        return self.stream


def _mic_stream(**kwargs: Any) -> tuple[MicStream, _FakeInputStream, _RecordingFactory]:
    """Build a MicStream wired to a fake InputStream.

    Defaults ``processors=[]`` (raw audio) so the many pre-existing tests in
    this file — which assert on exact chunk values / lengths and don't care
    about echo cancellation — aren't coupled to MicStream's real default (an
    ``EchoCanceller``, which needs the ``voice`` extra and a downloaded model).
    Tests that exercise the ``processors`` chain itself pass their own fakes
    explicitly, overriding this default via ``**kwargs``.
    """
    stream = _FakeInputStream(16000, 1, "int16", kwargs.get("blocksize", 512), None)
    factory = _RecordingFactory(stream)
    kwargs.setdefault("processors", [])
    ms = MicStream(input_stream_factory=factory, **kwargs)
    return ms, stream, factory


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0, interval: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Every MicStream this file opens must close itself; this is a safety net."""
    yield
    _mic_registry._streams.clear()


# ----------------------------------------------------------------------
# open() / close()
# ----------------------------------------------------------------------
def test_open_calls_factory_with_expected_args_and_becomes_open() -> None:
    ms, stream, factory = _mic_stream(sample_rate=16000, blocksize=512, device_key="k-open")
    ms.open()
    try:
        assert ms.is_open
        assert stream.started
        call = factory.calls[0]
        assert call == {"samplerate": 16000, "channels": 1, "dtype": "int16", "blocksize": 512, "device": None}
    finally:
        ms.close()


def test_open_is_idempotent() -> None:
    ms, _stream, factory = _mic_stream(device_key="k-idempotent")
    ms.open()
    try:
        ms.open()  # second call is a no-op
        assert ms.is_open
        assert len(factory.calls) == 1
    finally:
        ms.close()


def test_open_raises_and_leaves_registry_clean_when_factory_fails() -> None:
    def bad_factory(*_args: object) -> _FakeInputStream:
        raise OSError("no such device")

    ms = MicStream(input_stream_factory=bad_factory, device_key="k-fail", processors=[])
    with pytest.raises(RuntimeError, match="Cannot open mic stream"):
        ms.open()
    assert not ms.is_open
    assert _mic_registry.get("k-fail") is None


def test_open_raises_when_second_stream_shares_device_key() -> None:
    ms1, _s1, _f1 = _mic_stream(device_key="k-dup")
    ms2, _s2, _f2 = _mic_stream(device_key="k-dup")
    ms1.open()
    try:
        with pytest.raises(RuntimeError):
            ms2.open()
        assert not ms2.is_open
    finally:
        ms1.close()


def test_open_succeeds_concurrently_for_different_device_keys() -> None:
    ms1, _s1, _f1 = _mic_stream(device_key="k-a")
    ms2, _s2, _f2 = _mic_stream(device_key="k-b")
    ms1.open()
    ms2.open()
    try:
        assert ms1.is_open
        assert ms2.is_open
    finally:
        ms1.close()
        ms2.close()


def test_close_is_idempotent() -> None:
    ms, _stream, _factory = _mic_stream(device_key="k-close-idempotent")
    ms.open()
    ms.close()
    assert not ms.is_open
    ms.close()  # second call is a no-op, no error
    assert not ms.is_open


def test_close_releases_registry_entry() -> None:
    ms, _stream, _factory = _mic_stream(device_key="k-registry")
    ms.open()
    assert _mic_registry.get("k-registry") is ms
    ms.close()
    assert _mic_registry.get("k-registry") is None


# ----------------------------------------------------------------------
# subscribe() fan-out / drop-oldest / close termination
# ----------------------------------------------------------------------
def _collect_markers(sub: Subscription, markers: list[int], timeout: float = 2.0) -> list[int]:
    """Drain *sub* until every value in *markers* (in order) was seen.

    Filler chunks are always negative (see _FakeInputStream.read) so they are
    silently skipped here — this makes the assertion robust to the background
    thread racing ahead with filler before/between the test's own pushes.
    """
    want = list(markers)
    seen: list[int] = []
    deadline = time.monotonic() + timeout
    while want and time.monotonic() < deadline:
        item = sub.get(timeout=max(0.0, deadline - time.monotonic()))
        if item is None:
            continue
        value = int(item[0])
        if value == want[0]:
            seen.append(value)
            want.pop(0)
    return seen


def test_subscribers_receive_the_same_chunk_sequence() -> None:
    ms, stream, _factory = _mic_stream(device_key="k-fanout")
    sub_a = ms.subscribe()
    sub_b = ms.subscribe()
    ms.open()
    try:
        stream.push_chunk([1] * 512)
        stream.push_chunk([2] * 512)
        stream.push_chunk([3] * 512)

        assert _collect_markers(sub_a, [1, 2, 3]) == [1, 2, 3]
        assert _collect_markers(sub_b, [1, 2, 3]) == [1, 2, 3]
    finally:
        ms.close()


def test_full_subscription_queue_drops_oldest_and_keeps_latest() -> None:
    ms, stream, _factory = _mic_stream(device_key="k-drop")
    sub = ms.subscribe(maxsize=2)
    ms.open()
    try:
        for v in (1, 2, 3, 4, 5):
            stream.push_chunk([v] * 512)

        assert _wait_until(lambda: sub.dropped >= 3)
        # Drain synchronously (no timeout wait) to minimize the race window
        # against the background thread's continuing filler production —
        # whatever real (positive) markers remain must end with the newest.
        remaining = []
        while True:
            try:
                item = sub._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                remaining.append(int(item[0]))
        positive = [v for v in remaining if v > 0]
        assert positive
        assert positive[-1] == 5
        assert sub.dropped >= 3
    finally:
        ms.close()


def test_close_ends_subscription_iteration() -> None:
    ms, stream, _factory = _mic_stream(device_key="k-iter")
    sub = ms.subscribe()
    ms.open()
    try:
        stream.push_chunk([9] * 512)
        assert _wait_until(lambda: sub._queue.qsize() >= 1)
    finally:
        ms.close()
    # After close, iterating the subscription must terminate (StopIteration),
    # not hang — draining whatever was queued and then stopping at the sentinel.
    chunks = list(sub)
    assert isinstance(chunks, list)


def test_stream_iterator_stops_after_close() -> None:
    ms, _stream, _factory = _mic_stream(device_key="k-stream")
    ms.open()
    it = ms.stream()
    ms.close()
    # Either already exhausted, or exhausts on the next pull — never hangs.
    list(it)


def test_stream_returns_a_subscription_not_a_plain_iterator() -> None:
    ms, _stream, _factory = _mic_stream(device_key="k-stream-type")
    ms.open()
    try:
        sub = ms.stream()
        assert isinstance(sub, Subscription)
        # Still directly iterable, so an existing `for chunk in stream.stream():` is unaffected.
        assert iter(sub) is sub
    finally:
        ms.close()


def test_stream_subscription_close_unsubscribes() -> None:
    ms, stream, _factory = _mic_stream(device_key="k-stream-close")
    ms.open()
    try:
        sub = ms.stream()
        stream.push_chunk([11] * 512)
        assert _collect_markers(sub, [11]) == [11]
        sub.close()
        assert sub not in ms._subs
    finally:
        ms.close()


def test_stream_passes_maxsize_through_to_subscribe() -> None:
    ms, _stream, _factory = _mic_stream(device_key="k-stream-maxsize")
    ms.open()
    try:
        sub = ms.stream(maxsize=2)
        assert sub._queue.maxsize == 2
    finally:
        ms.close()


def test_subscription_as_context_manager_returns_self() -> None:
    ms, _stream, _factory = _mic_stream(device_key="k-with-self")
    ms.open()
    try:
        sub = ms.stream()
        with sub as entered:
            assert entered is sub
    finally:
        ms.close()


def test_subscription_context_manager_unsubscribes_on_exit() -> None:
    ms, stream, _factory = _mic_stream(device_key="k-with-exit")
    ms.open()
    try:
        with ms.stream() as sub:
            stream.push_chunk([13] * 512)
            assert _collect_markers(sub, [13]) == [13]
        assert sub not in ms._subs
        # After exiting the `with`, no more chunks reach this subscription —
        # it's unsubscribed, so the capture thread no longer writes to it.
        stream.push_chunk([14] * 512)
        time.sleep(0.05)
        assert sub._queue.qsize() == 0
    finally:
        ms.close()


def test_subscription_context_manager_exit_is_idempotent_after_manual_close() -> None:
    ms, _stream, _factory = _mic_stream(device_key="k-with-double-close")
    ms.open()
    try:
        with ms.stream() as sub:
            sub.close()
            # sub already closed itself inside the `with` block; __exit__
            # calling close() again on the way out must not raise.
    finally:
        ms.close()


def test_close_clears_subs_and_stale_subscription_does_not_see_next_generations_audio() -> None:
    ms, stream, _factory = _mic_stream(device_key="k-reopen-stale-sub")
    stale_sub = ms.subscribe()
    ms.open()
    ms.close()
    # The sentinel ended iteration for the pre-close subscriber (draining
    # whatever filler chunks preceded it, if any, same as other tests here).
    list(stale_sub)
    assert ms._subs == []

    # ...and after a fresh open(), only a fresh subscribe() sees the new audio.
    ms.open()
    try:
        stream.push_chunk([77] * 512)
        fresh_sub = ms.subscribe()
        assert _collect_markers(fresh_sub, [77]) == [77]
        # The stale subscription (from before close()) must never have received it.
        remaining = []
        while True:
            try:
                item = stale_sub._queue.get_nowait()
            except queue.Empty:
                break
            remaining.append(item)
        assert all(item is None or int(item[0]) != 77 for item in remaining)
    finally:
        ms.close()


# ----------------------------------------------------------------------
# record()
# ----------------------------------------------------------------------
def test_record_returns_correctly_formatted_wav_bytes() -> None:
    ms, _stream, _factory = _mic_stream(sample_rate=16000, device_key="k-record")
    ms.open()
    try:
        audio = ms.record(0.2)
    finally:
        ms.close()
    assert audio is not None
    with wave.open(BytesIO(audio), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 16000
        assert wf.getsampwidth() == 2
        assert wf.getnframes() >= round(16000 * 0.2)


def test_record_returns_none_when_stream_is_not_open() -> None:
    ms, _stream, _factory = _mic_stream(device_key="k-record-closed")
    assert ms.record(0.2) is None


def test_record_returns_none_when_stream_closes_mid_capture() -> None:
    import threading

    ms, _stream, _factory = _mic_stream(device_key="k-record-race")

    class _NeverDeliversStream(_FakeInputStream):
        def read(self, n: int) -> tuple[np.ndarray, bool]:
            time.sleep(0.05)
            raise RuntimeError("device gone")

    never = _NeverDeliversStream(16000, 1, "int16", 512, None)
    ms._input_stream_factory = lambda *a: never
    ms.open()
    # Close almost immediately; record() should observe the closed stream
    # (via the sentinel) rather than hang until its own timeout.
    closer = threading.Timer(0.05, ms.close)
    closer.start()
    try:
        audio = ms.record(5.0)
    finally:
        closer.join()
    assert audio is None
    assert not ms.is_open


# ----------------------------------------------------------------------
# Read-failure backoff / recovery
# ----------------------------------------------------------------------
def test_read_failures_backoff_then_recover_and_resume_dispatch() -> None:
    ms, stream, _factory = _mic_stream(device_key="k-backoff")
    sub = ms.subscribe()
    ms.open()
    try:
        stream.push_error(RuntimeError("usb hiccup"))
        stream.push_error(RuntimeError("usb hiccup again"))
        stream.push_chunk([42] * 512)
        assert _collect_markers(sub, [42]) == [42]
        assert ms.is_open  # the background thread survived both failures
    finally:
        ms.close()


# ----------------------------------------------------------------------
# Generation separation / lifecycle lock (join timeout doesn't strand
# subscribers or let a new open() silently race the stale thread).
# ----------------------------------------------------------------------
class _WedgedReadStream(_FakeInputStream):
    """A stream whose read() never returns — simulates a USB read wedged forever.

    The background thread that calls this stays alive (daemon) past close(),
    exactly the scenario close()'s join-timeout branch exists to handle.
    """

    def read(self, n: int) -> tuple[np.ndarray, bool]:
        threading.Event().wait()  # blocks forever; never set
        raise AssertionError("unreachable")  # pragma: no cover


def test_close_join_timeout_still_unblocks_subscribers_and_next_open_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mic_stream_module, "_JOIN_TIMEOUT_S", 0.05)
    stream = _WedgedReadStream(16000, 1, "int16", 512, None)
    ms = MicStream(input_stream_factory=lambda *a: stream, device_key="k-join-timeout", processors=[])
    sub = ms.subscribe()
    ms.open()

    ms.close()  # join times out (thread wedged in read()); leaks the thread
    assert not ms.is_open
    assert _mic_registry.get("k-join-timeout") is None

    # (a) A subscriber blocked in iteration must still terminate — close()'s
    # timeout branch delivers the sentinel itself rather than waiting on the
    # wedged thread's own finally.
    chunks = list(sub)
    assert isinstance(chunks, list)

    # (b) A later open() must refuse to start a second generation while the
    # stale thread (still bound to the OLD stream/stop pair) is alive, rather
    # than silently letting it coexist with a new capture thread.
    with pytest.raises(RuntimeError):
        ms.open()
    assert not ms.is_open
    assert _mic_registry.get("k-join-timeout") is None  # guard fires before registering


def test_close_join_timeout_thread_releases_stream_itself_once_unblocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wedged thread — not close() — must stop/close the stream once it recovers.

    Synchronized via threading.Event (no sleep polling): `release` unblocks
    the fake stream's read() (simulating the USB read finally returning), and
    `stopped` fires the instant the thread's own finally calls stream.stop().
    """
    monkeypatch.setattr(mic_stream_module, "_JOIN_TIMEOUT_S", 0.05)
    release = threading.Event()
    stopped = threading.Event()

    class _ReleasableWedgedStream(_FakeInputStream):
        def read(self, n: int) -> tuple[np.ndarray, bool]:
            release.wait()
            raise RuntimeError("device gone (simulated) after being unblocked")

        def stop(self) -> None:
            super().stop()
            stopped.set()

    stream = _ReleasableWedgedStream(16000, 1, "int16", 512, None)
    ms = MicStream(input_stream_factory=lambda *a: stream, device_key="k-release", processors=[])
    ms.open()
    thread = ms._thread
    assert thread is not None

    ms.close()  # join times out (still blocked in read()); close() must not touch the stream
    assert not ms.is_open
    assert not stream.stopped
    assert not stream.closed

    release.set()  # unblock read() -> it raises -> the loop notices stop is set and exits
    assert stopped.wait(timeout=2.0), "the recovered thread never stopped its own stream"
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert stream.stopped
    assert stream.closed


def test_open_rolls_back_when_thread_start_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    ms, stream, _factory = _mic_stream(device_key="k-thread-fail")

    def _raising_thread(*_args: object, **_kwargs: object) -> threading.Thread:
        raise OSError("can't start new thread")

    monkeypatch.setattr(mic_stream_module.threading, "Thread", _raising_thread)

    with pytest.raises(RuntimeError, match="Cannot open mic stream"):
        ms.open()

    assert not ms.is_open
    assert _mic_registry.get("k-thread-fail") is None
    # The stream was already start()ed before the thread failed to launch;
    # rollback must stop/close it rather than leaking the device handle.
    assert stream.stopped
    assert stream.closed


# ----------------------------------------------------------------------
# record() argument validation
# ----------------------------------------------------------------------
@pytest.mark.parametrize("seconds", [0, -1, -0.5])
def test_record_raises_value_error_for_non_positive_seconds(seconds: float) -> None:
    ms, _stream, _factory = _mic_stream(device_key="k-record-invalid")
    with pytest.raises(ValueError, match="seconds must be positive"):
        ms.record(seconds)


# ----------------------------------------------------------------------
# processors — fake AudioProcessor implementations (WAV in/out, per the
# AudioProcessor protocol), exercising the real int16_to_wav/wav_to_int16
# round trip on each call rather than faking that away.
# ----------------------------------------------------------------------
class _ScaleProcessor:
    """Multiplies every sample by *scale*; records each call's input WAV."""

    def __init__(self, scale: int) -> None:
        self.scale = scale
        self.calls: list[bytes] = []

    def process(self, wav: bytes) -> bytes:
        self.calls.append(wav)
        samples, sample_rate = wav_to_int16(wav)
        scaled = (samples.astype(np.int32) * self.scale).clip(-32768, 32767).astype(np.int16)
        return int16_to_wav(scaled, sample_rate)


class _EmptyProcessor:
    """Always returns a 0-frame WAV — simulates a streaming processor still buffering."""

    def process(self, wav: bytes) -> bytes:
        _samples, sample_rate = wav_to_int16(wav)
        return int16_to_wav(np.array([], dtype=np.int16), sample_rate)


class _RaisingProcessor:
    """Always raises — simulates a processor bug/crash."""

    def process(self, wav: bytes) -> bytes:
        raise RuntimeError("boom")


def _wait_for_chunk_value(sub: Subscription, value: int, timeout: float = 2.0) -> np.ndarray | None:
    """Drain *sub* until a chunk whose first sample equals *value* arrives, or timeout.

    Filler chunks from ``_FakeInputStream`` are always negative, so a
    positive *value* is unambiguous even while the background thread races
    ahead producing filler.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = sub.get(timeout=max(0.0, deadline - time.monotonic()))
        if chunk is None:
            continue
        if int(chunk[0]) == value:
            return chunk
    return None


def test_processors_empty_list_passes_raw_audio_untouched() -> None:
    ms, stream, _factory = _mic_stream(device_key="k-proc-raw", processors=[])
    sub = ms.subscribe()
    ms.open()
    try:
        stream.push_chunk([99] * 512)
        assert _wait_for_chunk_value(sub, 99) is not None
    finally:
        ms.close()


def test_processor_transforms_dispatched_chunks() -> None:
    proc = _ScaleProcessor(scale=2)
    ms, stream, _factory = _mic_stream(device_key="k-proc-scale", processors=[proc])
    sub = ms.subscribe()
    ms.open()
    try:
        stream.push_chunk([10] * 512)
        chunk = _wait_for_chunk_value(sub, 20)
        assert chunk is not None
        assert all(int(v) == 20 for v in chunk)
    finally:
        ms.close()


def test_two_processors_cascade_in_order() -> None:
    add_then_scale = _AddProcessor(delta=1)
    scale = _ScaleProcessor(scale=10)
    ms, stream, _factory = _mic_stream(device_key="k-proc-cascade", processors=[add_then_scale, scale])
    sub = ms.subscribe()
    ms.open()
    try:
        stream.push_chunk([5] * 512)
        # (5 + 1) * 10 = 60 if applied in the given order; 5*10 + 1 = 51 would
        # be the (wrong) reversed-order result — asserting 60 pins the order.
        chunk = _wait_for_chunk_value(sub, 60)
        assert chunk is not None
    finally:
        ms.close()


class _AddProcessor:
    """Adds *delta* to every sample; used together with _ScaleProcessor to pin cascade order."""

    def __init__(self, delta: int) -> None:
        self.delta = delta

    def process(self, wav: bytes) -> bytes:
        samples, sample_rate = wav_to_int16(wav)
        out = (samples.astype(np.int32) + self.delta).clip(-32768, 32767).astype(np.int16)
        return int16_to_wav(out, sample_rate)


def test_processor_returning_empty_wav_is_not_dispatched() -> None:
    ms, stream, _factory = _mic_stream(device_key="k-proc-empty", processors=[_EmptyProcessor()])
    sub = ms.subscribe()
    ms.open()
    try:
        stream.push_chunk([42] * 512)
        # The marker (42) never survives the empty-output processor, so it
        # must never be seen — only (negative) filler chunks, if anything.
        assert _wait_for_chunk_value(sub, 42, timeout=0.3) is None
    finally:
        ms.close()


def test_processor_exception_discards_chunk_and_keeps_stream_running(caplog: pytest.LogCaptureFixture) -> None:
    ms, stream, _factory = _mic_stream(device_key="k-proc-raise", processors=[_RaisingProcessor()])
    sub = ms.subscribe()
    with caplog.at_level("WARNING"):
        ms.open()
        try:
            stream.push_chunk([7] * 512)
            assert _wait_for_chunk_value(sub, 7, timeout=0.3) is None
            assert ms.is_open
            assert ms._thread is not None and ms._thread.is_alive()
        finally:
            ms.close()
    assert any("processor" in r.message.lower() for r in caplog.records)


def test_open_raises_when_default_echo_canceller_sample_rate_mismatches_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MicStream(sample_rate=48000) with the default EchoCanceller must fail fast at open().

    Without this check, open() would succeed and every chunk on the capture
    thread would then hit EchoCanceller.process's own ValueError (16000 !=
    48000), discarded silently forever — no audio, no obvious error. A fake
    EchoCanceller (with the same read-only ``sample_rate = 16000`` attribute
    the real one carries) stands in so this test needs no ai-edge-litert /
    model download.
    """

    class _FakeEchoCanceller:
        sample_rate = 16000
        capture_channels = 6

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def process(self, wav: bytes) -> bytes:  # pragma: no cover - never reached
            return wav

    monkeypatch.setattr("palmimo_sdk.audio.aec.EchoCanceller", _FakeEchoCanceller)

    stream = _FakeInputStream(48000, 6, "int16", 512, None)
    factory = _RecordingFactory(stream)
    ms = MicStream(input_stream_factory=factory, device_key="k-rate-mismatch", sample_rate=48000)

    with pytest.raises(RuntimeError, match="Cannot open mic stream"):
        ms.open()

    assert not ms.is_open
    assert _mic_registry.get("k-rate-mismatch") is None
    assert factory.calls == []  # resolved (and rejected) before the stream was ever built


def test_open_succeeds_with_sample_rate_mismatch_when_processors_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same sample_rate=48000 stream must open fine once echo cancellation is opted out of."""

    class _FakeEchoCanceller:
        sample_rate = 16000

        def __init__(self, *_args: object, **_kwargs: object) -> None:  # pragma: no cover - never constructed
            raise AssertionError("EchoCanceller must not be built when processors=[] is passed explicitly")

    monkeypatch.setattr("palmimo_sdk.audio.aec.EchoCanceller", _FakeEchoCanceller)

    ms, _stream, _factory = _mic_stream(device_key="k-rate-ok", sample_rate=48000, processors=[])
    ms.open()
    try:
        assert ms.is_open
    finally:
        ms.close()


def test_open_wraps_echo_canceller_construction_failure_in_runtime_error_with_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raising_echo_canceller(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("model download failed")

    monkeypatch.setattr("palmimo_sdk.audio.aec.EchoCanceller", _raising_echo_canceller)

    stream = _FakeInputStream(16000, 6, "int16", 512, None)
    factory = _RecordingFactory(stream)
    ms = MicStream(input_stream_factory=factory, device_key="k-echo-canceller-fail")  # processors default (None)

    with pytest.raises(RuntimeError, match="Cannot open mic stream"):
        ms.open()

    assert not ms.is_open
    assert _mic_registry.get("k-echo-canceller-fail") is None
    # Resolution happens before the stream is ever built — no device was touched.
    assert factory.calls == []


def test_default_processors_none_resolves_to_building_an_echo_canceller(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[Any] = []

    class _FakeEchoCanceller:
        sample_rate = 16000
        capture_channels = 1

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            built.append(self)

        def process(self, wav: bytes) -> bytes:  # pragma: no cover - never reached in this test
            return wav

    monkeypatch.setattr("palmimo_sdk.audio.aec.EchoCanceller", _FakeEchoCanceller)

    ms, _stream, _factory = _mic_stream(device_key="k-default-echo-canceller", processors=None)
    ms.open()
    try:
        assert ms.is_open
        assert len(built) == 1
    finally:
        ms.close()


def test_first_processors_capture_channels_reaches_the_input_stream_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeMultiChannelProcessor:
        sample_rate = 16000
        capture_channels = 6

        def process(self, wav: bytes) -> bytes:  # pragma: no cover - not exercised here
            return wav

    ms, _stream, factory = _mic_stream(device_key="k-capture-channels", processors=[_FakeMultiChannelProcessor()])
    ms.open()
    try:
        assert factory.calls[0]["channels"] == 6
    finally:
        ms.close()


def test_processor_declaring_multi_channel_outside_first_position_raises_at_open() -> None:
    class _FakeMultiChannelProcessor:
        sample_rate = 16000
        capture_channels = 3

        def process(self, wav: bytes) -> bytes:  # pragma: no cover - not exercised here
            return wav

    class _FakeMonoProcessor:
        sample_rate = 16000

        def process(self, wav: bytes) -> bytes:  # pragma: no cover - not exercised here
            return wav

    ms, _stream, factory = _mic_stream(
        device_key="k-capture-channels-not-first",
        processors=[_FakeMonoProcessor(), _FakeMultiChannelProcessor()],
    )

    with pytest.raises(RuntimeError, match="Cannot open mic stream"):
        ms.open()

    assert not ms.is_open
    assert factory.calls == []


def test_processors_empty_list_still_opens_with_one_channel() -> None:
    ms, _stream, factory = _mic_stream(device_key="k-capture-channels-raw", processors=[])
    ms.open()
    try:
        assert factory.calls[0]["channels"] == 1
    finally:
        ms.close()


# ----------------------------------------------------------------------
# Dispatched chunks are read-only (raw and processors paths alike)
# ----------------------------------------------------------------------
def test_dispatched_chunk_is_read_only_on_the_raw_path() -> None:
    ms, stream, _factory = _mic_stream(device_key="k-readonly-raw", processors=[])
    sub = ms.subscribe()
    ms.open()
    try:
        stream.push_chunk([5] * 512)
        chunk = _wait_for_chunk_value(sub, 5)
        assert chunk is not None
        assert chunk.flags.writeable is False
    finally:
        ms.close()


def test_dispatched_chunk_is_read_only_on_the_processors_path() -> None:
    ms, stream, _factory = _mic_stream(device_key="k-readonly-proc", processors=[_ScaleProcessor(scale=1)])
    sub = ms.subscribe()
    ms.open()
    try:
        stream.push_chunk([6] * 512)
        chunk = _wait_for_chunk_value(sub, 6)
        assert chunk is not None
        assert chunk.flags.writeable is False
    finally:
        ms.close()


# ----------------------------------------------------------------------
# _resolve_device — hint matching against sd.query_devices()
# ----------------------------------------------------------------------
class _FakeSoundDevice:
    """Stand-in for the ``sounddevice`` module slice ``_resolve_device`` calls."""

    def __init__(self, devices: list[dict[str, object]] | None = None, raises: Exception | None = None) -> None:
        self._devices = devices or []
        self._raises = raises

    def query_devices(self) -> list[dict[str, object]]:
        if self._raises is not None:
            raise self._raises
        return self._devices


def _log() -> logging.Logger:
    import logging

    return logging.getLogger("test-resolve-device")


def test_resolve_device_returns_none_when_hint_is_none() -> None:
    assert mic_stream_module._resolve_device(_FakeSoundDevice(), None, _log()) is None


def test_resolve_device_returns_none_when_hint_is_empty_string() -> None:
    assert mic_stream_module._resolve_device(_FakeSoundDevice(), "", _log()) is None


def test_resolve_device_matches_hint_case_insensitively_against_an_input_device() -> None:
    devices = [
        {"name": "Built-in Microphone", "max_input_channels": 1},
        {"name": "ReSpeaker 4-Mic Array", "max_input_channels": 4},
    ]
    idx = mic_stream_module._resolve_device(_FakeSoundDevice(devices), "respeaker", _log())
    assert idx == 1


def test_resolve_device_returns_none_when_no_device_matches_the_hint() -> None:
    devices = [{"name": "Built-in Microphone", "max_input_channels": 1}]
    assert mic_stream_module._resolve_device(_FakeSoundDevice(devices), "nonexistent-device", _log()) is None


def test_resolve_device_ignores_a_name_match_with_zero_input_channels() -> None:
    # An output-only device sharing the hint's name substring must not match.
    devices = [{"name": "ReSpeaker Output", "max_input_channels": 0}]
    assert mic_stream_module._resolve_device(_FakeSoundDevice(devices), "respeaker", _log()) is None


def test_resolve_device_returns_none_when_query_devices_raises() -> None:
    assert (
        mic_stream_module._resolve_device(_FakeSoundDevice(raises=RuntimeError("no backend")), "respeaker", _log())
        is None
    )


# ----------------------------------------------------------------------
# _resolve_device — channel-aware resolution (min_channels / required_by)
# ----------------------------------------------------------------------
def test_resolve_device_skips_a_hint_match_with_too_few_channels_and_picks_a_later_sufficient_one() -> None:
    """Hint AND channel count are both required, not either/or.

    Device 1 matches the hint but has too few channels; device 2 has enough
    channels but does NOT match the hint; device 3 matches the hint AND has
    enough channels. Only device 3 may be selected.
    """
    devices = [
        {"name": "Built-in Microphone", "max_input_channels": 1},
        {"name": "ReSpeaker (low)", "max_input_channels": 2},
        {"name": "USB Audio (unrelated)", "max_input_channels": 8},
        {"name": "ReSpeaker 6-Mic Array", "max_input_channels": 6},
    ]
    idx = mic_stream_module._resolve_device(_FakeSoundDevice(devices), "respeaker", _log(), min_channels=6)
    assert idx == 3


def test_resolve_device_raises_when_no_device_has_enough_channels() -> None:
    devices = [
        {"name": "ReSpeaker (low)", "max_input_channels": 2},
        {"name": "Built-in Microphone", "max_input_channels": 1},
    ]
    with pytest.raises(RuntimeError) as exc_info:
        mic_stream_module._resolve_device(
            _FakeSoundDevice(devices), "respeaker", _log(), min_channels=6, required_by="EchoCanceller"
        )
    message = str(exc_info.value)
    assert "6" in message
    assert "processors=[Denoiser()]" in message
    assert "processors=[]" in message
    assert "EchoCanceller" in message


def test_resolve_device_min_channels_one_with_no_match_still_only_warns_and_returns_none() -> None:
    """Regression guard: the mono (min_channels=1, today's default) path must not start raising."""
    devices = [{"name": "Built-in Microphone", "max_input_channels": 1}]
    idx = mic_stream_module._resolve_device(_FakeSoundDevice(devices), "respeaker", _log(), min_channels=1)
    assert idx is None


# ----------------------------------------------------------------------
# _capture_channels — rejects an invalid first-processor capture_channels
# ----------------------------------------------------------------------
@pytest.mark.parametrize("bad_channels", [0, -1, -6, 1.5, "6", None])
def test_capture_channels_rejects_invalid_first_processor_channels(bad_channels: object) -> None:
    class _BadChannelsProcessor:
        capture_channels = bad_channels

        def process(self, wav: bytes) -> bytes:  # pragma: no cover - never reached
            return wav

    ms, _stream, _factory = _mic_stream(device_key=f"k-bad-channels-{bad_channels!r}")
    with pytest.raises(RuntimeError):
        ms._capture_channels([_BadChannelsProcessor()])


# ----------------------------------------------------------------------
# record() with a buffering processor (empty output, then a combined flush)
# ----------------------------------------------------------------------
def test_record_completes_with_a_buffering_processor_that_delays_then_flushes() -> None:
    """A processor that withholds output on odd calls and flushes combined output on even
    calls (mimicking a streaming denoiser's internal buffering) must not stall record():
    the samples eventually come out, just batched, and record() keeps waiting for enough
    of them rather than assuming one chunk in => one chunk out.
    """

    class _BufferingProcessor:
        def __init__(self) -> None:
            self._buffer: list[np.ndarray] = []
            self._calls = 0

        def process(self, wav: bytes) -> bytes:
            samples, sample_rate = wav_to_int16(wav)
            self._buffer.append(samples)
            self._calls += 1
            if self._calls % 2 != 0:
                return int16_to_wav(np.array([], dtype=np.int16), sample_rate)
            combined = np.concatenate(self._buffer)
            self._buffer = []
            return int16_to_wav(combined, sample_rate)

    ms, _stream, _factory = _mic_stream(
        sample_rate=16000, device_key="k-record-buffering", processors=[_BufferingProcessor()]
    )
    ms.open()
    try:
        audio = ms.record(0.2)
    finally:
        ms.close()
    assert audio is not None
    with wave.open(BytesIO(audio), "rb") as wf:
        assert wf.getnframes() >= round(16000 * 0.2)


# ----------------------------------------------------------------------
# Capture-path observability: overflows / blocks / late_blocks / process_seconds
# ----------------------------------------------------------------------
def test_overflowed_read_increments_overflows_counter() -> None:
    ms, stream, _factory = _mic_stream(device_key="k-overflow")
    ms.open()
    try:
        stream.push_chunk([1] * 512, overflowed=True)
        assert _wait_until(lambda: ms.overflows >= 1)
    finally:
        ms.close()
    assert ms.overflows == 1


def test_slow_processor_marks_blocks_late() -> None:
    """A processor slow relative to a tiny per-block budget must be counted late.

    blocksize=16 at sample_rate=16000 gives budget = 16/16000 = 1ms, so an
    8ms artificial sleep is unambiguously over LATE_BLOCK_FRACTION (0.8) of
    that budget -- deterministic, no flakiness from timing being "close".
    """

    class _SlowProcessor:
        def process(self, wav: bytes) -> bytes:
            time.sleep(0.008)
            return wav

    ms, stream, _factory = _mic_stream(
        device_key="k-late-block", sample_rate=16000, blocksize=16, processors=[_SlowProcessor()]
    )
    ms.open()
    try:
        for _ in range(3):
            stream.push_chunk([1] * 16)
        assert _wait_until(lambda: ms.late_blocks >= 1, timeout=5.0)
    finally:
        ms.close()
    assert ms.late_blocks >= 1
    assert ms.blocks >= 1
    assert ms.process_seconds > 0


def test_close_logs_capture_summary_when_blocks_were_processed(caplog: pytest.LogCaptureFixture) -> None:
    ms, stream, _factory = _mic_stream(device_key="k-summary-log")
    with caplog.at_level("INFO"):
        ms.open()
        try:
            stream.push_chunk([1] * 512)
            assert _wait_until(lambda: ms.blocks >= 1)
        finally:
            ms.close()
    assert any("blocks" in r.message and "overflow" in r.message for r in caplog.records)


# ----------------------------------------------------------------------
# End-to-end multi-channel capture: a real (frames, 6) block genuinely
# reaches the first processor and comes out mono to a subscriber -- not just
# that _build_stream was called with the right `channels` argument.
# ----------------------------------------------------------------------
def _multi_channel_block(frames: int, channels: int) -> np.ndarray:
    """A (frames, channels) int16 block where column c holds ``100 * c + row``."""
    rows = [[100 * channel + frame for channel in range(channels)] for frame in range(frames)]
    return np.array(rows, dtype=np.int16)


class _ColumnPickingProcessor:
    """Stand-in for EchoCanceller: decodes a multi-channel WAV, picks one column, re-encodes mono."""

    capture_channels = 6

    def __init__(self, column: int) -> None:
        self.column = column

    def process(self, wav: bytes) -> bytes:
        samples, sample_rate = wav_to_int16_multi(wav)
        return int16_to_wav(samples[:, self.column], sample_rate)


def test_six_channel_block_survives_run_to_dispatch_via_the_first_processor() -> None:
    proc = _ColumnPickingProcessor(column=3)
    ms, stream, factory = _mic_stream(device_key="k-six-channel-e2e", processors=[proc])
    sub = ms.subscribe()
    ms.open()
    try:
        assert factory.calls[0]["channels"] == 6
        block = _multi_channel_block(frames=16, channels=6)
        stream.push_chunk(block)
        expected = [100 * 3 + row for row in range(16)]

        deadline = time.monotonic() + 2.0
        seen: list[int] | None = None
        while time.monotonic() < deadline:
            chunk = sub.get(timeout=max(0.0, deadline - time.monotonic()))
            if chunk is None:
                continue
            values = [int(v) for v in chunk]
            if values == expected:
                seen = values
                break
        assert seen == expected
    finally:
        ms.close()
