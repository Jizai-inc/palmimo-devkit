"""Tests for :mod:`palmimo_companion_agent.realtime.services.audio` -- Playback + PitchShifter.

No real ``aplay``: ``subprocess.Popen`` is replaced with :class:`_FakePopen`,
a stand-in whose ``stdin.write`` can be made to block so a test can prove
:meth:`~palmimo_companion_agent.realtime.services.audio.Playback.write` and
:meth:`~palmimo_companion_agent.realtime.services.audio.Playback.interrupt`
return immediately regardless -- the whole point of the writer-thread rework.
``_FakeStdin`` also shares a real lock between ``write()`` and ``close()``,
mirroring a real ``io.BufferedWriter``'s internal lock, so a test can
reproduce the hazard of a writer thread wedged mid-write holding that lock.
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Any, ClassVar

import numpy as np
import pytest

from palmimo_companion_agent.realtime.services.audio import PitchShifter, Playback


class _FakeStdin:
    def __init__(self, write_delay: float = 0.0, block_until_killed: bool = False) -> None:
        self.chunks: list[bytes] = []
        self.closed = False
        self.close_raises = False
        self.write_delay = write_delay
        self.block_until_killed = block_until_killed
        #: A real lock, held for the duration of write() -- close() also
        #: acquires it, mirroring the internal lock a real
        #: io.BufferedWriter holds during a blocking write(). This is what
        #: lets a wedged-write test reproduce the real ordering hazard:
        #: whichever of write()/close() gets there first blocks the other
        #: until it is released.
        self.lock = threading.Lock()
        #: Set by _FakePopen.kill() -- only consulted by a write() configured
        #: with block_until_killed, standing in for the real write's EPIPE
        #: once the process feeding it dies.
        self.killed = threading.Event()

    def write(self, data: bytes) -> None:
        with self.lock:
            if self.closed:
                raise BrokenPipeError("stdin is closed")
            if self.block_until_killed:
                self.killed.wait()  # never returns on its own -- only kill() unblocks it
                raise BrokenPipeError("process was killed mid-write")
            if self.write_delay:
                time.sleep(self.write_delay)
            self.chunks.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        with self.lock:
            if self.close_raises:
                raise OSError("simulated close failure")
            self.closed = True


class _FakePopen:
    """Records what would have gone to ``aplay``; ``instances`` collects every one built."""

    instances: ClassVar[list[_FakePopen]] = []
    write_delay: ClassVar[float] = 0.0
    block_until_killed: ClassVar[bool] = False
    wait_timeouts_before_success: ClassVar[int] = 0

    def __init__(self, args: list[str], stdin: int | None = None) -> None:
        self.args = args
        self.stdin = _FakeStdin(write_delay=type(self).write_delay, block_until_killed=type(self).block_until_killed)
        self.killed = False
        self.wait_calls: list[float | None] = []
        self._poll_value: int | None = None
        self._remaining_timeouts = type(self).wait_timeouts_before_success
        type(self).instances.append(self)

    def poll(self) -> int | None:
        return self._poll_value

    def kill(self) -> None:
        self.killed = True
        self._poll_value = -9
        self.stdin.killed.set()

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self._remaining_timeouts > 0:
            self._remaining_timeouts -= 1
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout or 0)
        return 0


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch) -> type[_FakePopen]:
    _FakePopen.instances = []
    _FakePopen.write_delay = 0.0
    _FakePopen.block_until_killed = False
    _FakePopen.wait_timeouts_before_success = 0
    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    return _FakePopen


def _wait_until(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was never met")


# ----------------------------------------------------------------------
# write() never blocks the caller
# ----------------------------------------------------------------------


def test_write_returns_immediately_even_when_the_pipe_blocks(fake_popen: type[_FakePopen]) -> None:
    fake_popen.write_delay = 0.3
    playback = Playback()

    started = time.monotonic()
    playback.write(b"\x00\x00" * 100)
    elapsed = time.monotonic() - started

    assert elapsed < 0.1, f"write() blocked the caller for {elapsed:.2f}s"
    _wait_until(lambda: fake_popen.instances and fake_popen.instances[0].stdin.chunks)
    playback.close()


def test_written_chunks_reach_the_process_stdin_in_order(fake_popen: type[_FakePopen]) -> None:
    playback = Playback()

    playback.write(b"\x01\x02")
    playback.write(b"\x03\x04")
    _wait_until(lambda: fake_popen.instances and len(fake_popen.instances[0].stdin.chunks) == 2)

    assert fake_popen.instances[0].stdin.chunks == [b"\x01\x02", b"\x03\x04"]
    playback.close()


def test_write_pitch_shifts_before_writing(fake_popen: type[_FakePopen]) -> None:
    playback = Playback(pitch=PitchShifter(2.0))
    pcm = np.zeros(480, dtype=np.int16).tobytes()

    playback.write(pcm)
    _wait_until(lambda: fake_popen.instances and fake_popen.instances[0].stdin.chunks)

    written = fake_popen.instances[0].stdin.chunks[0]
    assert len(written) < len(pcm), "a 2x pitch shift should have produced fewer samples"
    playback.close()


def test_write_after_close_is_dropped_and_does_not_start_a_new_writer(fake_popen: type[_FakePopen]) -> None:
    playback = Playback()
    playback.write(b"\x00\x00")
    _wait_until(lambda: fake_popen.instances and fake_popen.instances[0].stdin.chunks)
    playback.close()

    writer_thread_count_before = threading.active_count()
    playback.write(b"\x01\x01")  # must be dropped -- close() already ran
    time.sleep(0.05)

    assert len(fake_popen.instances) == 1, "write() after close() must not spawn a fresh aplay"
    assert threading.active_count() <= writer_thread_count_before, "write() after close() must not start a writer"


# ----------------------------------------------------------------------
# Which output aplay is pointed at
# ----------------------------------------------------------------------


def test_playback_addresses_the_configured_alsa_device(fake_popen: type[_FakePopen]) -> None:
    playback = Playback(device="plughw:CARD=ArrayUAC10,DEV=0")

    playback.write(b"\x01\x02")
    _wait_until(lambda: bool(fake_popen.instances))

    argv = fake_popen.instances[0].args
    assert argv[argv.index("-D") + 1] == "plughw:CARD=ArrayUAC10,DEV=0"
    # -D has to precede the stream description aplay reads positionally.
    assert argv.index("-D") < argv.index("-t")
    playback.close()


def test_playback_omits_the_device_flag_when_none_is_configured(fake_popen: type[_FakePopen]) -> None:
    """No device must mean no ``-D`` at all, not ``-D ''`` -- ALSA's own
    default is the documented fallback and an empty argument is not it."""
    playback = Playback()

    playback.write(b"\x01\x02")
    _wait_until(lambda: bool(fake_popen.instances))

    assert "-D" not in fake_popen.instances[0].args
    playback.close()


# ----------------------------------------------------------------------
# interrupt() drops queued audio and kills without blocking the caller
# ----------------------------------------------------------------------


def test_interrupt_returns_immediately_even_when_the_pipe_blocks(fake_popen: type[_FakePopen]) -> None:
    fake_popen.write_delay = 0.3
    playback = Playback()
    playback.write(b"\x00\x00" * 100)  # picked up by the writer thread and blocks there
    _wait_until(lambda: fake_popen.instances)

    started = time.monotonic()
    playback.interrupt()
    elapsed = time.monotonic() - started

    assert elapsed < 0.1, f"interrupt() blocked the caller for {elapsed:.2f}s"
    _wait_until(lambda: fake_popen.instances[0].killed)
    playback.close()


def test_interrupt_drops_chunks_still_queued(fake_popen: type[_FakePopen]) -> None:
    fake_popen.write_delay = 0.2
    playback = Playback()
    playback.write(b"\x00\x00")  # picked up immediately, keeps the writer thread busy mid-write
    _wait_until(lambda: fake_popen.instances)
    playback.write(b"\x01\x01")  # queued behind the busy writer -- interrupt should drop this
    playback.write(b"\x02\x02")

    playback.interrupt()
    _wait_until(lambda: fake_popen.instances[0].killed)
    # The first write's own (slow, fake) stdin.write() was already past the
    # "am I closed" check when interrupt() ran, so it still lands -- wait out
    # its full delay before asserting nothing queued *after* it got through.
    time.sleep(fake_popen.write_delay + 0.1)

    assert fake_popen.instances[0].stdin.chunks == [b"\x00\x00"], "a queued chunk was played after interrupt()"
    playback.close()


def test_interrupt_reaps_the_killed_process(fake_popen: type[_FakePopen]) -> None:
    playback = Playback()
    playback.write(b"\x00\x00")
    _wait_until(lambda: fake_popen.instances and fake_popen.instances[0].stdin.chunks)

    playback.interrupt()

    _wait_until(lambda: fake_popen.instances[0].wait_calls)
    assert fake_popen.instances[0].killed is True
    playback.close()


def test_interrupt_is_a_no_op_when_nothing_is_playing(fake_popen: type[_FakePopen]) -> None:
    playback = Playback()
    playback.interrupt()  # must not raise
    assert fake_popen.instances == []
    playback.close()


def test_a_chunk_played_after_interrupt_starts_a_fresh_process(fake_popen: type[_FakePopen]) -> None:
    playback = Playback()
    playback.write(b"\x00\x00")
    _wait_until(lambda: fake_popen.instances and fake_popen.instances[0].stdin.chunks)

    playback.interrupt()
    _wait_until(lambda: fake_popen.instances[0].killed)

    playback.write(b"\x01\x01")
    _wait_until(lambda: len(fake_popen.instances) == 2 and fake_popen.instances[1].stdin.chunks)

    assert fake_popen.instances[1].stdin.chunks == [b"\x01\x01"]
    playback.close()


class _SlowPitch:
    """Duck-types :class:`PitchShifter` with a controllable delay, to widen the dequeue-to-write race window."""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    def process(self, pcm: bytes) -> bytes:
        time.sleep(self.delay)
        return pcm


def test_interrupt_mid_pitch_processing_drops_the_stale_chunk_without_spawning(fake_popen: type[_FakePopen]) -> None:
    """A chunk the writer dequeued just before interrupt() must not spawn/write once interrupt() has moved on.

    Regression for the race where the writer dequeues pre-barge-in audio,
    interrupt() clears ``_process`` and kills, and then the writer's
    ``_ensure_process()`` sees no process and spawns a *fresh* aplay for
    audio that should have been dropped -- playing it over the user
    mid-interruption. The generation counter is what closes this window.
    """
    playback = Playback(pitch=_SlowPitch(0.2))
    playback.write(b"\x00\x00")
    time.sleep(0.05)  # let the writer dequeue the chunk and enter (slow) pitch processing
    playback.interrupt()

    time.sleep(0.3)  # longer than the stale pitch-processing + write would have taken if unguarded

    assert fake_popen.instances == [], "a chunk dequeued before interrupt() must not spawn a fresh aplay"
    playback.close()


def test_a_healthy_interrupt_mid_write_does_not_trigger_the_silence_warning(
    fake_popen: type[_FakePopen], capsys: pytest.CaptureFixture[str]
) -> None:
    """Barge-in kills the process mid-write, which raises BrokenPipeError in the writer.

    That must not be mistaken for a dead audio device: it is the normal,
    expected outcome of every interruption, and must not spam the operator
    warning on every barge-in.
    """
    fake_popen.block_until_killed = True
    playback = Playback()
    playback.write(b"\x00\x00")  # writer picks this up and blocks in the fake write, holding the process
    _wait_until(lambda: fake_popen.instances)

    playback.interrupt()
    _wait_until(lambda: fake_popen.instances[0].killed)
    _wait_until(lambda: not playback._pending_kills)  # let the kill/reap thread finish and self-prune

    assert playback._write_failures == 0, "an interrupt-caused write failure must not count as a device failure"
    captured = capsys.readouterr().err
    assert "playback write failed" not in captured
    playback.close()


# ----------------------------------------------------------------------
# close() -- allowed to block, but must not abandon a live process
# ----------------------------------------------------------------------


def test_close_stops_the_writer_thread_and_reaps_the_process(fake_popen: type[_FakePopen]) -> None:
    playback = Playback()
    playback.write(b"\x00\x00")
    _wait_until(lambda: fake_popen.instances and fake_popen.instances[0].stdin.chunks)

    playback.close()

    assert fake_popen.instances[0].stdin.closed is True
    assert fake_popen.instances[0].wait_calls == [5]


def test_close_kills_and_waits_again_when_the_first_wait_times_out(fake_popen: type[_FakePopen]) -> None:
    fake_popen.wait_timeouts_before_success = 1
    playback = Playback()
    playback.write(b"\x00\x00")
    _wait_until(lambda: fake_popen.instances and fake_popen.instances[0].stdin.chunks)

    playback.close()

    process = fake_popen.instances[0]
    assert process.killed is True, "a live aplay was abandoned after wait() timed out"
    assert len(process.wait_calls) == 2


def test_close_still_reaps_when_stdin_close_raises(fake_popen: type[_FakePopen]) -> None:
    playback = Playback()
    playback.write(b"\x00\x00")
    _wait_until(lambda: fake_popen.instances and fake_popen.instances[0].stdin.chunks)
    fake_popen.instances[0].stdin.close_raises = True

    playback.close()

    assert fake_popen.instances[0].wait_calls == [5], "a raise in stdin.close() must not skip wait()"


def test_close_with_nothing_ever_played_does_not_raise(fake_popen: type[_FakePopen]) -> None:
    Playback().close()
    assert fake_popen.instances == []


def test_close_leaves_no_writer_thread_behind(fake_popen: type[_FakePopen]) -> None:
    before = threading.active_count()
    playback = Playback()
    playback.write(b"\x00\x00")
    _wait_until(lambda: fake_popen.instances and fake_popen.instances[0].stdin.chunks)

    playback.close()

    _wait_until(lambda: threading.active_count() <= before + 1)  # allow the OS/pytest's own bookkeeping slack


def test_close_with_a_backlog_stops_promptly_without_playing_it_all(fake_popen: type[_FakePopen]) -> None:
    """A long backlog must not delay close(): it is discarded, not drained through the writer."""
    fake_popen.write_delay = 0.05
    playback = Playback()
    playback.write(b"\x00\x00")  # picked up immediately, keeps the writer busy for write_delay
    _wait_until(lambda: fake_popen.instances)
    for i in range(1, 50):
        playback.write(bytes([i, i]))  # queued behind the busy first write

    started = time.monotonic()
    playback.close()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"close() took {elapsed:.2f}s draining a backlog it should have discarded"
    # Only the in-flight chunk (already past the writer's queue.get() when
    # close() ran) gets written; everything still queued is dropped.
    assert len(fake_popen.instances[0].stdin.chunks) == 1


def test_no_process_is_spawned_for_a_straggler_chunk_after_close(fake_popen: type[_FakePopen]) -> None:
    """A chunk the writer dequeues right as close() runs must be dropped, not spawn a fresh aplay."""
    fake_popen.write_delay = 0.1
    playback = Playback()
    playback.write(b"\x00\x00")  # writer picks this up, blocks in the fake write for write_delay
    _wait_until(lambda: fake_popen.instances)
    playback.write(b"\x01\x01")  # sits in the queue until close() discards it

    playback.close()

    assert len(fake_popen.instances) == 1, "close() must never let a straggler chunk start a fresh aplay"


def test_no_respawn_after_close_even_when_the_writer_wins_a_race(fake_popen: type[_FakePopen]) -> None:
    """Directly exercises _ensure_process()'s closed check, independent of queue-drain timing."""
    playback = Playback()
    playback.write(b"\x00\x00")
    _wait_until(lambda: fake_popen.instances and fake_popen.instances[0].stdin.chunks)

    playback.close()
    assert fake_popen.instances[0].stdin.closed is True

    assert playback._ensure_process(playback._generation) is None
    assert len(fake_popen.instances) == 1


def test_close_kills_before_closing_stdin_so_a_wedged_write_cannot_hang_it(fake_popen: type[_FakePopen]) -> None:
    """BLOCKER regression: close() must kill() before stdin.close(), or a wedged writer hangs it forever.

    The fake's stdin shares a real ``threading.Lock`` between ``write()``
    and ``close()``, exactly like a real ``io.BufferedWriter``'s internal
    lock. The write is configured to block on that lock indefinitely --
    never returning on its own -- until ``kill()`` fires the "killed" event,
    mirroring the hazard the reviewer reproduced: a writer thread wedged
    inside ``stdin.write()`` (ALSA backpressure) holds the lock, so calling
    ``stdin.close()`` before ``kill()`` would block the calling thread
    forever, and the kill+wait ladder after it would never even run.

    With ``process.kill()`` called first, the wedged write unblocks with a
    (simulated) ``BrokenPipeError`` almost immediately, freeing the lock so
    ``stdin.close()`` and ``wait()`` can proceed -- ``close()`` finishes in
    roughly the writer thread's own 5s join timeout, not indefinitely.
    Swapping the ordering back (``stdin.close()`` before ``kill()``) hangs
    this test instead of it finishing within the assertion's bound, because
    ``stdin.close()`` would then block on the lock forever.
    """
    fake_popen.block_until_killed = True
    playback = Playback()
    playback.write(b"\x00\x00")
    _wait_until(lambda: fake_popen.instances)

    started = time.monotonic()
    playback.close()
    elapsed = time.monotonic() - started

    process = fake_popen.instances[0]
    assert process.killed is True, "close() never reached kill() -- it is stuck ahead of it, same as the real hang"
    assert elapsed < 7.0, f"close() took {elapsed:.2f}s -- kill() must run before stdin.close(), not after"


def test_close_supervises_a_pending_interrupt_kill_thread_that_has_not_finished(fake_popen: type[_FakePopen]) -> None:
    """interrupt() hands its process to a fire-and-forget kill thread; close() right after must still supervise it.

    Simulates the moment right after interrupt() has tracked a kill/reap
    thread in ``_pending_kills`` but that thread has not run yet -- the
    window close() must not treat as "nothing to supervise" just because
    ``_process`` is already ``None`` by the time it looks.
    """
    playback = Playback()
    playback.write(b"\x00\x00")
    _wait_until(lambda: fake_popen.instances and fake_popen.instances[0].stdin.chunks)
    process = fake_popen.instances[0]

    release = threading.Event()

    def _still_running() -> None:
        release.wait()  # blocks well past close()'s own bounded join, until the test releases it

    stub_thread = threading.Thread(target=_still_running, daemon=True)
    with playback._process_lock:
        playback._process = None
        playback._pending_kills.append((stub_thread, process))
    stub_thread.start()

    playback.close()

    assert process.killed is True, "close() must take over and kill a process left with a still-running kill thread"
    release.set()


def test_interrupt_then_close_is_idempotent_and_does_not_deadlock(fake_popen: type[_FakePopen]) -> None:
    playback = Playback()
    playback.write(b"\x00\x00")
    _wait_until(lambda: fake_popen.instances and fake_popen.instances[0].stdin.chunks)

    playback.interrupt()
    _wait_until(lambda: fake_popen.instances[0].killed)
    playback.close()  # must not raise, hang, or spawn anything new

    assert len(fake_popen.instances) == 1


def test_close_then_interrupt_is_idempotent_and_does_not_deadlock(fake_popen: type[_FakePopen]) -> None:
    playback = Playback()
    playback.write(b"\x00\x00")
    _wait_until(lambda: fake_popen.instances and fake_popen.instances[0].stdin.chunks)

    playback.close()
    playback.interrupt()  # must not raise, hang, or spawn anything new

    assert len(fake_popen.instances) == 1


# ----------------------------------------------------------------------
# write failures are not suppressed forever
# ----------------------------------------------------------------------


def _raise_on_write(_data: bytes) -> None:
    raise OSError("simulated device failure")


def test_first_failed_write_warns_once(fake_popen: type[_FakePopen], capsys: pytest.CaptureFixture[str]) -> None:
    playback = Playback()
    playback.write(b"\x00\x00")
    _wait_until(lambda: fake_popen.instances)
    fake_popen.instances[0].stdin.write = _raise_on_write  # type: ignore[assignment]

    playback.write(b"\x01\x01")
    playback.write(b"\x02\x02")
    _wait_until(lambda: playback._write_failures >= 2)

    captured = capsys.readouterr().err
    assert captured.count("playback write failed") == 1
    playback.close()


def test_persistent_write_failures_warn_once_then_go_quiet(
    fake_popen: type[_FakePopen], capsys: pytest.CaptureFixture[str]
) -> None:
    playback = Playback()
    playback.write(b"\x00\x00")
    _wait_until(lambda: fake_popen.instances)
    fake_popen.instances[0].stdin.write = _raise_on_write  # type: ignore[assignment]

    for i in range(3):
        playback.write(bytes([i, i]))
    _wait_until(lambda: playback._write_failures >= 3)

    captured = capsys.readouterr().err
    assert captured.count("playback write failed") == 1, "failures 2 and 3 must stay quiet"
    playback.close()


def test_write_failure_warning_recurs_after_the_warn_interval(
    fake_popen: type[_FakePopen], capsys: pytest.CaptureFixture[str]
) -> None:
    playback = Playback()
    playback.write(b"\x00\x00")
    _wait_until(lambda: fake_popen.instances)
    fake_popen.instances[0].stdin.write = _raise_on_write  # type: ignore[assignment]

    for i in range(playback._WARN_EVERY + 1):
        playback._queue.put_nowait(bytes([i % 256, i % 256]))
    _wait_until(lambda: playback._write_failures >= playback._WARN_EVERY + 1)

    captured = capsys.readouterr().err
    assert captured.count("playback write failed") == 2, "the Nth consecutive failure must warn again"
    playback.close()


def test_a_successful_write_resets_the_failure_counter(
    fake_popen: type[_FakePopen], capsys: pytest.CaptureFixture[str]
) -> None:
    playback = Playback()
    playback.write(b"\x00\x00")
    _wait_until(lambda: fake_popen.instances)
    real_write = fake_popen.instances[0].stdin.write
    fake_popen.instances[0].stdin.write = _raise_on_write  # type: ignore[assignment]

    playback.write(b"\x01\x01")
    _wait_until(lambda: playback._write_failures == 1)
    capsys.readouterr()  # discard the first warning

    fake_popen.instances[0].stdin.write = real_write  # type: ignore[method-assign]
    playback.write(b"\x02\x02")
    _wait_until(lambda: playback._write_failures == 0)

    fake_popen.instances[0].stdin.write = _raise_on_write  # type: ignore[assignment]
    playback.write(b"\x03\x03")
    _wait_until(lambda: playback._write_failures == 1)

    captured = capsys.readouterr().err
    assert captured.count("playback write failed") == 1, "a reset counter must warn again on the very next failure"
    playback.close()


class _RaisingPitch:
    """Duck-types :class:`PitchShifter` but always fails, to exercise pitch-processing failure handling."""

    def process(self, pcm: bytes) -> bytes:
        raise ValueError("malformed chunk")


def test_a_pitch_processing_failure_is_counted_and_does_not_kill_the_writer_thread(
    fake_popen: type[_FakePopen], capsys: pytest.CaptureFixture[str]
) -> None:
    """A pitch.process() exception must be handled like any other playback failure, not crash the writer thread.

    An uncaught exception here would kill the writer thread via the default
    excepthook; the next write() would silently start a brand new one via
    _ensure_writer(), with no trace that anything had gone wrong. Proving
    the thread survives means checking its identity stays the same, not
    just that playback keeps working.
    """
    playback = Playback(pitch=_RaisingPitch())

    playback.write(b"\x00\x00")
    _wait_until(lambda: playback._write_failures >= 1)
    writer_before = playback._writer

    captured = capsys.readouterr().err
    assert "playback write failed" in captured
    assert fake_popen.instances == [], "a pitch failure must never reach _ensure_process"

    playback._pitch = None  # let the next chunk process cleanly
    playback.write(b"\x01\x01")
    _wait_until(lambda: fake_popen.instances and fake_popen.instances[0].stdin.chunks)

    assert fake_popen.instances[0].stdin.chunks == [b"\x01\x01"]
    assert playback._writer is writer_before, "the writer thread must not have crashed and been silently replaced"
    playback.close()


# ----------------------------------------------------------------------
# PitchShifter
# ----------------------------------------------------------------------


def test_pitch_shifter_at_1x_still_processes(fake_popen: type[_FakePopen]) -> None:
    shifter = PitchShifter(1.0)
    pcm = np.array([100, -100, 200, -200], dtype=np.int16).tobytes()
    out = shifter.process(pcm)
    assert len(out) > 0


def test_pitch_shifter_raises_pitch_by_shrinking_the_chunk() -> None:
    shifter = PitchShifter(2.0)
    pcm = np.zeros(480, dtype=np.int16).tobytes()
    out = shifter.process(pcm)
    assert len(out) < len(pcm)


def test_pitch_shifter_handles_an_empty_chunk() -> None:
    shifter = PitchShifter(1.15)
    assert shifter.process(b"") == b""
