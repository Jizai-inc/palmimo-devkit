# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Speaker I/O resource — the SDK's non-blocking text-to-speech orchestration.

Like the other io/ resources, the SDK owns the hardware-facing tool and higher
layers just hand it text. :class:`Speaker` itself is engine-agnostic: it owns
the queue/worker/barge-in/timeout machinery and delegates all actual
synthesis to a :class:`~palmimo_sdk.io.tts.base.TtsEngine` (default
:class:`~palmimo_sdk.io.tts.piper.PiperEngine`, piper-plus (MIT)). Loading a
voice model is the expensive part of speech (~1.3s on a Pi: ONNX session
creation); synthesis is fast (~0.2-0.4s). :meth:`Speaker.open` therefore loads
the default-language voice in-process via the engine and keeps it resident
(cached per language) for the life of the ``Speaker``; each :meth:`say` only
synthesizes and plays the WAV through a short-lived playback subprocess
(``aplay``/``afplay``, the same tools piper's own ``--auto-play`` uses).

Speech is non-blocking: :meth:`Speaker.say` / :meth:`Speaker.say_bilingual`
enqueue a job and return immediately; a single resident worker thread speaks
them one at a time (see :class:`Speaker`'s docstring for the ownership
model). The default engine module imports piper-plus lazily so
``import palmimo_sdk`` stays dependency-free.

Audio-output setup lives in ``README.md``; piper-plus specifics, including
where voice models are cached and when they are downloaded, live in
:mod:`palmimo_sdk.io.tts.piper`.
"""

from __future__ import annotations

import logging
import os
import platform
import queue
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .alsa_devices import resolve_alsa_device
from .tts import PiperEngine, TtsEngine, TtsVoice


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpeakerConfig:
    """Engine-agnostic speech settings.

    ``lang`` is the default voice (``"en"``/``"ja"``). ``bilingual_gap_s`` is
    the pause between the two halves of :meth:`Speaker.say_bilingual`.
    ``say_timeout_s`` is one utterance's whole budget (voice reload if needed
    + synthesis + playback). Playback (a subprocess) is hard-killed on
    timeout, but that failure alone does not evict the voice -- only a
    synthesis failure or timeout does (in-process synthesis has no
    cancellation hook and is abandoned on its own thread instead, see
    :meth:`Speaker._synthesize_with_timeout`), since the voice is what a
    playback failure never touches. An evicted language is reloaded, with a
    logged warning, on its next :meth:`say`.

    ``device_name_hint`` names the playback device by substring (e.g.
    ``"ReSpeaker"``) instead of leaving the choice to ALSA. ``None`` (the
    default) plays to whatever ALSA considers default. Set it when the machine
    has more than one output and the speech has to come out of a particular
    one -- and note that echo cancellation depends on it: the loopback channel
    :class:`~palmimo_sdk.audio.aec.EchoCanceller` reads carries only what the
    microphone array itself played, so speech sent elsewhere leaves the
    canceller with nothing to cancel against. Linux only (ALSA ``aplay``);
    macOS ``afplay`` has no device flag and ignores it.

    Engine-specific settings (piper-plus model names, ``data_dir``,
    ``length_scale``, ``volume``, ...) live on the :class:`~palmimo_sdk.io.tts.base.TtsEngine`
    passed to :class:`Speaker`'s ``engine=`` (e.g.
    :class:`~palmimo_sdk.io.tts.piper.PiperEngine`), not here. Audio setup:
    ``README.md``.
    """

    lang: str = "en"
    bilingual_gap_s: float = 0.3
    # One utterance's whole budget (see docstring above).
    say_timeout_s: float = 30.0
    device_name_hint: str | None = None


# The probe's throwaway phrase: short so probe synthesis stays fast, non-empty
# so piper actually runs its phonemization/inference path instead of a
# trivial no-op.
_PROBE_TEXT = "ok"

# How long a terminate()-then-reap waits before escalating to kill() (see
# Speaker._reap). A Player (real or fake) that ignores terminate() must not
# wedge the worker forever; a few seconds is generous for a real process to
# exit cleanly but still bounded.
_REAP_TIMEOUT_S = 3.0


def _with_alsa_device(argv: list[str], device: str | None) -> list[str]:
    """Return *argv* addressed at *device*, or unchanged when it cannot be.

    Only ALSA's ``aplay`` takes a ``-D``; ``play`` (sox) selects through
    ``AUDIODEV`` and ``ffplay``/``afplay`` through options that differ per
    build, so those are left alone rather than guessed at. A caller that needs
    a specific device on those players should set it in the environment.
    """
    if device is None or not argv or argv[0] != "aplay":
        return argv
    return [argv[0], "-D", device, *argv[1:]]


def _playback_argv_candidates(wav_path: Path) -> list[list[str]]:
    """Return the playback commands to try, in order, for the current
    platform — the same tools piper's own ``--auto-play`` shells out to
    (``aplay``/``play``/``ffplay`` on Linux, ``afplay`` on macOS), so
    audio-output setup (README.md) still applies unchanged.
    """
    path = str(wav_path)
    system = platform.system()
    if system == "Darwin":
        return [["afplay", path]]
    if system == "Linux":
        return [["aplay", path], ["play", path], ["ffplay", "-nodisp", "-autoexit", path]]
    return []


class _PlaybackProcess(Protocol):
    """The process-like handle a :class:`Player` returns from :meth:`Player.start`.

    Modeled on the subset of ``subprocess.Popen`` :meth:`Speaker._run_playback`
    needs, so the real player (:class:`_SubprocessPlayer`) and a test fake are
    driven identically -- no separate real-vs-injected code path.
    """

    returncode: int | None

    def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


class Player(Protocol):
    """Test seam for playback (default: :class:`_SubprocessPlayer`, a real
    subprocess). A fake implementation lets tests drive playback -- success,
    failure, a missing player binary, or a cancellable in-flight play --
    without touching real audio hardware."""

    def start(self, argv: list[str]) -> _PlaybackProcess: ...


class _SubprocessPlayer:
    """Default :class:`Player`: runs *argv* as a real subprocess."""

    def start(self, argv: list[str]) -> _PlaybackProcess:
        import subprocess  # lazy: keeps `import palmimo_sdk` dependency-free

        return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@dataclass
class _PlaybackResult:
    """The subset of ``subprocess.CompletedProcess`` :meth:`Speaker._play` reads."""

    returncode: int
    stderr: bytes


class _SpeechStoppedError(Exception):
    """Raised internally by :meth:`Speaker._synthesize_with_timeout` when a
    :class:`SpeechHandle` stop lands mid-synthesis. Caught by
    :meth:`Speaker._speak_once`, which returns quietly without treating it
    as a synthesis failure (no voice eviction, no warning log)."""


@dataclass
class _CachedVoice:
    """A loaded voice plus the lock that serializes calls into its
    ``.synthesize()`` (see :meth:`Speaker._synthesize_to_tmp_wav`).

    A stop or timeout abandons the synth thread instead of killing it (no
    ONNX cancellation hook) -- it keeps running, holding this lock, until it
    finishes on its own. Keeping the lock on the ``_CachedVoice`` rather than
    on the ``Speaker`` means a fresh voice loaded after eviction gets a fresh
    lock too, so it is never blocked by an abandoned call on the voice it
    replaced.
    """

    voice: TtsVoice
    lock: threading.Lock


class SpeechHandle:
    """One :meth:`Speaker.say` / :meth:`Speaker.say_bilingual` job.

    A failure is recorded on :attr:`error` instead of raising, so a caller
    that joins can tell speech from silence; a caller that ignores the handle
    keeps the fire-and-forget behaviour (the failure is only logged, see
    :meth:`Speaker._speak`). :meth:`stop` interrupts this one job -- see
    :class:`Speaker`'s docstring for how the worker honors it.
    """

    def __init__(self, speaker: Speaker) -> None:
        self.error: Exception | None = None
        self._speaker = speaker
        self._done = threading.Event()
        self._stop_event = threading.Event()

    @property
    def stop_requested(self) -> bool:
        """Whether :meth:`stop` has been called for this job."""
        return self._stop_event.is_set()

    def is_alive(self) -> bool:
        """Whether this job is still queued or in progress."""
        return not self._done.is_set()

    def join(self, timeout: float | None = None) -> None:
        """Block until this job finishes, or *timeout* seconds elapse."""
        self._done.wait(timeout)

    def stop(self) -> None:
        """Interrupt this job. If the worker is currently on it, terminates
        in-flight playback (an in-progress synthesis instead aborts via
        :meth:`Speaker._synthesize_with_timeout`'s own poll) and the worker
        finishes the job as usual. Otherwise -- still queued, or already
        finished -- there is nothing for the worker to interrupt, so this
        marks it done immediately instead of leaving a caller's :meth:`join`
        waiting for the worker to eventually reach (and skip) it. Idempotent.
        """
        self._stop_event.set()
        self._speaker._handle_stop(self)

    def _wait_stop(self, timeout: float) -> bool:
        """Block up to *timeout* seconds; returns ``True`` if :meth:`stop`
        fired meanwhile. Used for the bilingual gap so a stop wakes it
        immediately instead of sleeping it out."""
        return self._stop_event.wait(timeout)

    def _mark_done(self) -> None:
        self._done.set()


@dataclass
class _Job:
    """One worker unit: a :class:`SpeechHandle` plus the utterances it
    speaks in order (one for :meth:`Speaker.say`, two for
    :meth:`Speaker.say_bilingual`).

    ``enqueued_at_stop_count`` is :attr:`Speaker._stop_count` as of
    :meth:`Speaker._enqueue` -- see :meth:`Speaker._run_job` for why a job
    needs to remember this.
    """

    handle: SpeechHandle
    utterances: list[tuple[str, str]]
    enqueued_at_stop_count: int = 0


class Speaker:
    """Non-blocking, engine-agnostic text-to-speech with English/Japanese support.

    Owns the queue/worker/barge-in/timeout/playback machinery; all actual
    synthesis is delegated to a :class:`~palmimo_sdk.io.tts.base.TtsEngine`
    (``engine=``, defaults to :class:`~palmimo_sdk.io.tts.piper.PiperEngine`).

    Lifecycle: :meth:`open` (idempotent — loads the default-language voice
    once and probes it with a real, silent synthesis) / :meth:`say` /
    :meth:`close`. Usable as a context manager. :meth:`say` and
    :meth:`say_bilingual` are non-blocking and return the :class:`SpeechHandle`
    doing the speaking (callers can ignore it, or ``join`` it and then read
    its ``error``).

    Voices load lazily (once per language, cached for the ``Speaker``'s
    life); a synthesis failure or timeout evicts that language's voice, and
    the next :meth:`say` reloads it with a logged warning, so a wedged ONNX
    session never sticks around.

    Ownership model: a single resident daemon worker thread (started lazily
    by the first :meth:`say`/:meth:`say_bilingual`, restarted the same way
    after :meth:`close`, see :meth:`_enqueue`) is the only thread that ever
    synthesizes or plays. Each call enqueues one :class:`_Job` and returns
    its :class:`SpeechHandle` immediately; the worker drains the queue in
    order, so utterances are naturally serialized -- no per-utterance
    thread. :meth:`stop` marks every not-yet-started job's handle stopped
    and done (so a caller ``join``ing a queued job doesn't wait for jobs
    ahead of it to run first) and interrupts whichever job the worker is
    currently on, the same way :meth:`SpeechHandle.stop` does for a single
    handle. :attr:`_lock` only guards the voice cache (``_voices``,
    ``_ever_loaded``) and :meth:`open`'s bookkeeping -- utterance ordering is
    the worker's job and synthesis serialization is each voice's own job
    (see :class:`_CachedVoice`), not this lock's.

    :attr:`_worker_lock` is shared, and held across its *entire* operation,
    by :meth:`_enqueue` and :meth:`close`: this is what keeps the
    worker/sentinel handshake atomic against a concurrent enqueue -- a
    ``say()`` racing a ``close()`` either reaches the still-live worker
    before ``close()`` gets the lock, or waits for ``close()`` to finish
    (stop, sentinel, join, clear) and starts a fresh worker afterward. It
    can never land on a worker that has already consumed its exit sentinel
    (which would otherwise orphan the job -- nothing left to ever mark its
    handle done) or race two ``close()`` calls into both joining/clearing
    the same worker.

    Test seams: ``engine`` (a :class:`~palmimo_sdk.io.tts.base.TtsEngine`,
    defaults to a fresh :class:`~palmimo_sdk.io.tts.piper.PiperEngine`; a fake
    engine's :meth:`~palmimo_sdk.io.tts.base.TtsEngine.load_voice` can return
    any object with ``.synthesize(text, wav_file)``) and ``player`` (a
    :class:`Player`, defaults to :class:`_SubprocessPlayer`; used only for
    playback).
    """

    def __init__(
        self,
        config: SpeakerConfig | None = None,
        *,
        engine: TtsEngine | None = None,
        player: Player | None = None,
    ) -> None:
        self.config = config or SpeakerConfig()
        self._engine: TtsEngine = engine or PiperEngine()
        self._player: Player = player or _SubprocessPlayer()
        self._lock = threading.RLock()
        self._opened = False
        # Resolved from config.device_name_hint on first playback, not at
        # construction: a Speaker is routinely built before its USB device is
        # attached. Only a hit is remembered -- a miss is exactly what
        # attaching the device fixes, and caching it would outlast the
        # condition that produced it.
        self._alsa_device: str | None = None
        # Resident, per-language voice cache -- the fix for the fixed-cost
        # model-load-per-utterance latency (see module docstring). Each
        # entry also owns the lock that serializes synthesis on it (see
        # _CachedVoice).
        self._voices: dict[str, _CachedVoice] = {}
        self._ever_loaded: set[str] = set()
        # Resident worker + its job queue (see class docstring's ownership
        # model). `_worker` is None until the first say()/say_bilingual();
        # `_worker_lock` guards the whole enqueue/teardown handshake, not
        # just the `_worker` reference (see class docstring).
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        # Guards the worker's currently in-flight handle/subprocess, so a
        # SpeechHandle.stop() from any thread can find and terminate live
        # playback (see _handle_stop). Also guards `_stop_count` (see
        # `_run_job`'s stop-epoch check) -- one lock for both, since they're
        # read/written together at the same points.
        self._proc_lock = threading.Lock()
        self._current_handle: SpeechHandle | None = None
        self._current_proc: _PlaybackProcess | None = None
        # Bumped by every stop() call; lets _run_job tell a job enqueued
        # before a stop() (and already popped off the queue, so stop()'s own
        # drain can't see it) apart from one enqueued after.
        self._stop_count = 0

    @property
    def is_open(self) -> bool:
        return self._opened

    def _get_voice(self, lang: str) -> _CachedVoice:
        """Return the cached voice for *lang*, loading (and caching) it on
        first use via ``self._engine``. A voice previously evicted by a
        failed/timed-out synthesis is reloaded here transparently, with a
        logged warning."""
        cached = self._voices.get(lang)
        if cached is not None:
            return cached
        if lang in self._ever_loaded:
            logger.warning("%s voice (lang=%s) was unavailable; reloading it now", self._engine.name, lang)
        voice = self._engine.load_voice(lang)
        cached = _CachedVoice(voice=voice, lock=threading.Lock())
        self._voices[lang] = cached
        self._ever_loaded.add(lang)
        return cached

    def open(self) -> None:
        """Preflight the default voice, then load it and probe it with a
        real, silent synthesis; idempotent.

        Before loading anything, runs ``self._engine.preflight(config.lang)``.
        This is where an engine fetches a model it doesn't have on disk yet —
        a large write plus a network fetch, done here at a predictable moment
        rather than implicitly during an utterance — and where being offline
        surfaces with a remediation hint. If the default voice's preflight
        fails, this raises immediately without ever loading it. Only the
        *default* voice (``config.lang``) is fetched, and only it is required:
        the *other* language (needed by ``say(lang=...)`` overrides and
        ``say_bilingual``) is checked with ``fetch=False`` and merely logs a
        warning when missing — so a deliberately single-language setup neither
        fails nor pays to download a voice it will never speak.

        Once preflight passes, this loads the default voice (the engine's
        session-creation cost — the ~1.3s cost this module exists to pay only
        once) and probes it with the same synthesis path :meth:`say` uses, so
        a missing runtime dependency (e.g. NLTK data for piper's English
        phonemization) — which would otherwise only surface as a
        background-thread failure during a live utterance — is caught here
        instead, at connect time. Nothing is played; the probe's audio is
        synthesized to a throwaway temp file and discarded.

        Raises :class:`RuntimeError` if the default voice's preflight fails,
        or the probe synthesis fails; the message includes a remediation hint
        from ``self._engine.failure_hint``.
        """
        if self._opened:
            return
        self._engine.preflight(self.config.lang)
        other_lang = "en" if self.config.lang == "ja" else "ja"
        try:
            # fetch=False: only the voice being opened with is worth paying a
            # download for here. The other language is checked so a missing
            # one is reported now rather than mid-utterance -- nothing
            # downloads it later, since load_voice() never fetches.
            self._engine.preflight(other_lang, fetch=False)
        except RuntimeError as exc:
            logger.warning(
                "%s voice (lang=%s) is unavailable; say(lang=...) / say_bilingual will fail until fixed: %s",
                self._engine.name,
                other_lang,
                exc,
            )

        def probe_error(prefix: str, exc: Exception) -> RuntimeError:
            error_text = f"{type(exc).__name__}: {exc}"
            hint = self._engine.failure_hint(error_text, self.config.lang)
            return RuntimeError(f"{prefix}: {error_text}{hint}")

        with self._lock:
            try:
                cached = self._get_voice(self.config.lang)
            except RuntimeError:
                raise
            except Exception as exc:
                raise probe_error(f"{self._engine.name} is not available", exc) from exc
        try:
            wav_path = self._synthesize_with_timeout(cached, _PROBE_TEXT, self.config.say_timeout_s)
        except Exception as exc:
            with self._lock:
                self._voices.pop(self.config.lang, None)
            raise probe_error(f"{self._engine.name} probe synthesis failed", exc) from exc
        wav_path.unlink(missing_ok=True)
        self._opened = True

    def _synthesize_to_tmp_wav(self, cached: _CachedVoice, text: str) -> Path:
        """Synthesize *text* into a fresh temp WAV, holding *cached*'s lock
        for the actual ``.synthesize()`` call -- this is what serializes
        synthesis per voice (see :class:`_CachedVoice`), including against an
        abandoned call left running by a stop or timeout on the SAME voice.
        Runs on the synth worker thread spawned by
        :meth:`_synthesize_with_timeout`, so this lock can stay held for as
        long as synthesis takes without blocking the rest of the worker.
        """
        fd, path_str = tempfile.mkstemp(prefix="palmimo-speaker-", suffix=".wav")
        os.close(fd)
        path = Path(path_str)
        try:
            with cached.lock, wave.open(str(path), "wb") as wav_file:
                cached.voice.synthesize(text, wav_file)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path

    def _synthesize_with_timeout(
        self, cached: _CachedVoice, text: str, timeout: float, stop: SpeechHandle | None = None
    ) -> Path:
        """Synthesize *text* with *cached*'s voice, bounding the call to
        *timeout* seconds. Synthesis runs in-process (ONNX Runtime has no
        cancellation hook), so on timeout the call raises ``TimeoutError``
        but the synthesis itself keeps running to completion on an abandoned
        daemon thread, still holding *cached*'s lock (see
        :meth:`_synthesize_to_tmp_wav`) until it finishes; the caller
        (:meth:`say` / :meth:`open`) treats the timeout the same as any other
        synthesis failure — the voice is evicted and reloaded next time.

        *stop*, if given, is polled in short slices instead of a single
        blocking join, so a concurrent :meth:`SpeechHandle.stop` is noticed
        within one slice rather than only after the whole synthesis (up to
        *timeout*) completes -- otherwise the worker would stay on this job
        for that whole span before it could move to the next one. A stop
        mid-synthesis raises :class:`_SpeechStoppedError` instead of
        ``TimeoutError``, which :meth:`_speak_once` does NOT treat as a
        synthesis failure (no voice eviction) -- the abandoned thread still
        holds *cached*'s lock, so the next job on this (non-evicted) voice
        waits for it there instead of running concurrently.
        """
        outcome: dict[str, Any] = {}
        # Set when the caller stops waiting; a late-finishing worker then
        # deletes its own WAV instead of parking it where nobody reads it.
        # `decision_lock` closes the TOCTOU between the two sides checking/
        # setting `gave_up` and deciding the WAV's fate.
        gave_up = threading.Event()
        decision_lock = threading.Lock()

        def _run() -> None:
            try:
                path = self._synthesize_to_tmp_wav(cached, text)
            except Exception as exc:  # broad on purpose: reported to the joining call below
                outcome["error"] = exc
                return
            with decision_lock:
                if gave_up.is_set():
                    path.unlink(missing_ok=True)
                else:
                    outcome["path"] = path

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        poll_interval = 0.05
        deadline = time.monotonic() + timeout
        while worker.is_alive():
            if stop is not None and stop.stop_requested:
                with decision_lock:
                    gave_up.set()
                raise _SpeechStoppedError()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(timeout=min(poll_interval, remaining))
        if worker.is_alive():
            with decision_lock:
                gave_up.set()
            raise TimeoutError(f"{self._engine.name} synthesis exceeded {timeout}s")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["path"]

    def _play(self, wav_path: Path, timeout: float, handle: SpeechHandle | None = None) -> None:
        if handle is not None and handle.stop_requested:
            return  # stopped between synthesis finishing and playback starting
        candidates = _playback_argv_candidates(wav_path)
        if not candidates:
            raise RuntimeError(f"no known audio player for platform {platform.system()!r}")
        # Only aplay can be pointed at a device (see _with_alsa_device), so a
        # platform whose candidates do not include it must not pay for the
        # lookup: on macOS resolution would fork a missing `aplay -l` and warn
        # about it, once per utterance, to produce a value the argv rewrite
        # then discards.
        #
        # What the lookup does spend comes out of this utterance's budget
        # rather than extending it -- `aplay -l` has its own multi-second
        # ceiling and say_timeout_s is documented as the whole thing. Charged
        # on every utterance that misses, since a miss is deliberately not
        # memoized.
        lookup_started = time.monotonic()
        device = self._playback_device() if any(argv[0] == "aplay" for argv in candidates) else None
        timeout = max(0.0, timeout - (time.monotonic() - lookup_started))
        last_exc: Exception | None = None
        for args in (_with_alsa_device(argv, device) for argv in candidates):
            try:
                result = self._run_playback(args, timeout=timeout, handle=handle)
            except FileNotFoundError as exc:
                last_exc = exc
                continue
            if result is None:
                return  # terminated by stop() — a deliberate interruption, not a failure
            if getattr(result, "returncode", 0):
                stderr = (getattr(result, "stderr", b"") or b"").decode("utf-8", "replace").strip()
                raise RuntimeError(f"audio playback ({args[0]}) exited {result.returncode}: {stderr[-300:]}")
            return
        raise RuntimeError(f"no audio player available ({candidates}): {last_exc}")

    def _playback_device(self) -> str | None:
        """The ALSA device this speaker plays to.

        ``None`` means "issue the command without a device", which is both the
        no-hint case and the hint-did-not-match one -- ALSA's own default then
        applies, as it did before this setting existed.

        A resolved device is remembered; a miss is not, so a speaker built
        before its USB device was attached picks it up on a later utterance
        rather than keeping the answer from before it existed. With no hint
        nothing is looked up at all, so retrying costs nothing there.
        """
        if self._alsa_device is None:
            self._alsa_device = resolve_alsa_device(self.config.device_name_hint, kind="playback", log=logger)
        return self._alsa_device

    def _reap(self, proc: _PlaybackProcess) -> None:
        """Wait for *proc* to exit after :meth:`~_PlaybackProcess.terminate`,
        escalating to :meth:`~_PlaybackProcess.kill` if it doesn't within
        :data:`_REAP_TIMEOUT_S` -- a Player (real or fake) that ignores
        ``terminate()`` must not wedge the worker forever. The reap after
        ``kill()`` itself stays unbounded: a real SIGKILL cannot be ignored."""
        import subprocess  # lazy: keeps `import palmimo_sdk` dependency-free

        try:
            proc.communicate(timeout=_REAP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()

    def _run_playback(self, args: list[str], timeout: float, handle: SpeechHandle | None) -> Any:
        """Run one playback candidate through :attr:`_player`.

        Returns ``None`` if *handle* was stopped mid-playback (a deliberate
        interruption, handled by :meth:`_play`), otherwise a result exposing
        ``.returncode`` and ``.stderr``. The in-flight process is recorded on
        ``self._current_proc`` (guarded by ``self._proc_lock``) for the
        duration of the call, so a concurrent :meth:`SpeechHandle.stop` can
        reach in and terminate it (see :meth:`_handle_stop`).
        """
        proc = self._player.start(args)  # may raise FileNotFoundError -- handled by _play
        with self._proc_lock:
            stopped_before_start = handle is not None and handle.stop_requested
            if not stopped_before_start:
                self._current_proc = proc
        if stopped_before_start:
            proc.terminate()
            self._reap(proc)
            return None
        import subprocess  # lazy: keeps `import palmimo_sdk` dependency-free

        try:
            _, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        finally:
            with self._proc_lock:
                self._current_proc = None
        if handle is not None and handle.stop_requested:
            return None
        if proc.returncode is None:
            # A Player protocol violation, not a normal nonzero-exit
            # failure: `None or 0` would otherwise silently read as success.
            raise RuntimeError(f"player returned no returncode for {args!r} after communicate()")
        return _PlaybackResult(returncode=proc.returncode, stderr=stderr or b"")

    def _handle_stop(self, handle: SpeechHandle) -> None:
        """Called by :meth:`SpeechHandle.stop`. If *handle* is the job the
        worker is currently on, terminates its in-flight playback (a
        mid-synthesis stop is instead noticed by
        :meth:`_synthesize_with_timeout`'s own poll -- there's no live
        process to terminate yet). Otherwise -- still queued, or already
        finished -- there's nothing for the worker to interrupt right now,
        so this marks it done immediately instead of leaving a caller's
        :meth:`~SpeechHandle.join` waiting for the worker to eventually
        dequeue (and skip) it."""
        with self._proc_lock:
            is_current = self._current_handle is handle
            proc = self._current_proc if is_current else None
        if proc is not None:
            proc.terminate()
        if not is_current:
            handle._mark_done()

    def _speak_once(self, text: str, lang: str, handle: SpeechHandle | None = None) -> None:
        """Synthesize *text* in *lang* (loading/reusing the resident voice)
        and play it, raising on failure. One deadline of ``say_timeout_s``
        bounds synthesis + playback (voice lookup is cheap and excluded, so
        lock contention there can't eat into it).

        *handle* is the :class:`SpeechHandle` the worker is speaking this
        utterance for, so a barge-in :meth:`SpeechHandle.stop` short-circuits
        synthesis-in-progress (see :meth:`_synthesize_with_timeout`) and
        not-yet-started playback (see :meth:`_play`).

        Voice lookup and eviction-on-failure are guarded by ``self._lock``
        (just the cache); synthesis itself is serialized per-voice instead
        (see :class:`_CachedVoice`), so an abandoned stop/timeout thread only
        blocks its own (about to be evicted, or not) voice, never a
        different one.
        """
        text = text.strip()
        if not text:
            return
        if handle is not None and handle.stop_requested:
            return
        with self._lock:
            cached = self._get_voice(lang)
        deadline = time.monotonic() + self.config.say_timeout_s
        try:
            wav_path = self._synthesize_with_timeout(cached, text, max(0.0, deadline - time.monotonic()), stop=handle)
        except _SpeechStoppedError:
            # A deliberate stop, not a synthesis failure -- the voice
            # stays cached, unlike the eviction below.
            return
        except Exception:
            # The voice may be in a bad state (or just slow/hung); evict it
            # so the next say() for this language reloads it from scratch.
            with self._lock:
                self._voices.pop(lang, None)
            raise
        if handle is not None and handle.stop_requested:
            wav_path.unlink(missing_ok=True)
            return
        try:
            remaining = max(0.0, deadline - time.monotonic())
            self._play(wav_path, timeout=remaining, handle=handle)
        finally:
            wav_path.unlink(missing_ok=True)

    def _speak(self, text: str, lang: str, handle: SpeechHandle) -> None:
        # Log and swallow, so one failed utterance in a job (e.g. the English
        # half of a bilingual announcement) still lets a later one attempt to
        # speak; the first error wins on the handle.
        try:
            self._speak_once(text, lang, handle)
        except Exception as exc:
            logger.warning("%s TTS failed: %s", self._engine.name, exc, exc_info=True)
            if handle.error is None:
                handle.error = exc

    def _worker_loop(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:  # sentinel: close() asked the worker to exit
                return
            try:
                self._run_job(job)
            except Exception:
                # Insurance: _run_job's own finally already marks the
                # handle done, but if a future change makes it raise before
                # that runs, the worker must survive to keep draining the
                # queue rather than silently dying.
                logger.exception("unexpected error in speech worker job")

    def _run_job(self, job: _Job) -> None:
        handle = job.handle
        with self._proc_lock:
            # A job the worker has already dequeued is invisible to stop()'s
            # queue drain (it isn't in the queue anymore) and not yet
            # `_current_handle` either -- so a stop() landing in exactly this
            # window would otherwise be missed and the job would speak in
            # full. Comparing stop-epochs under the same lock stop() bumps
            # closes that window: a job enqueued before a stop() that hasn't
            # reached the worker yet is honored as stopped, not spoken.
            if self._stop_count > job.enqueued_at_stop_count:
                handle._stop_event.set()
                handle._mark_done()
                return
            self._current_handle = handle
        try:
            for i, (text, lang) in enumerate(job.utterances):
                if handle.stop_requested:
                    break
                if i > 0 and handle._wait_stop(self.config.bilingual_gap_s):
                    break  # stopped during the bilingual gap
                self._speak(text, lang, handle)
        finally:
            with self._proc_lock:
                self._current_handle = None
                self._current_proc = None
            handle._mark_done()

    def _enqueue(self, job: _Job) -> None:
        """Start the worker if it isn't running and hand it *job*, both
        under ``_worker_lock`` -- see :class:`Speaker`'s docstring for why
        this must be atomic with :meth:`close`."""
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._worker_loop, daemon=True)
                self._worker.start()
            with self._proc_lock:
                job.enqueued_at_stop_count = self._stop_count
            self._queue.put(job)

    def say(self, text: str, lang: str | None = None) -> SpeechHandle:
        """Speak ``text`` on the resident worker (non-blocking).

        Join the returned handle and read its ``error`` to find out whether
        it actually spoke. :meth:`stop` (or :meth:`Speaker.stop`) can
        interrupt it mid-utterance.
        """
        voice = lang or self.config.lang
        handle = SpeechHandle(self)
        self._enqueue(_Job(handle=handle, utterances=[(text, voice)]))
        return handle

    def say_bilingual(self, text_en: str, text_ja: str) -> SpeechHandle:
        """Speak English then Japanese, with a short gap (non-blocking).

        Both halves run as one worker job, so -- unlike two separate
        ``say()`` calls -- another ``say()`` can never land in the gap and
        split the announcement.
        """
        handle = SpeechHandle(self)
        self._enqueue(_Job(handle=handle, utterances=[(text_en, "en"), (text_ja, "ja")]))
        return handle

    def stop(self) -> None:
        """Stop any in-flight or queued speech immediately (barge-in);
        idempotent, and a no-op when nothing is speaking or queued.

        Drains every not-yet-started job from the queue, marking each
        handle stopped and done (so a caller joining a queued job isn't left
        waiting for jobs ahead of it to run first), then interrupts whichever
        job the worker is currently on -- see :class:`SpeechHandle.stop`.

        If :meth:`close`'s exit sentinel is sitting in the queue, draining
        pops it too; it's re-queued once the drain finishes (the queue is
        empty at that point, so this preserves both its presence and its
        position) instead of being discarded -- dropping it would leave the
        worker blocked on ``self._queue.get()`` forever, with nothing left
        to ever wake it, hanging :meth:`close`'s ``worker.join()``. This can
        still happen even though ``close()`` now holds ``_worker_lock``
        across its own sentinel/join sequence: a *different*, external
        ``stop()`` call doesn't take that lock, so it can still interleave.
        """
        sentinel_seen = False
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break
            if job is None:
                sentinel_seen = True
                continue
            job.handle.stop()
            job.handle._mark_done()
        if sentinel_seen:
            self._queue.put(None)
        with self._proc_lock:
            self._stop_count += 1
            current = self._current_handle
        if current is not None:
            current.stop()

    def close(self) -> None:
        """Tear down the resident voice cache and stop the worker; idempotent.
        A subsequent :meth:`say`/:meth:`open` reloads whichever language it
        needs and restarts the worker (see :meth:`_enqueue`).

        Truncates whatever is currently speaking or queued (via :meth:`stop`)
        rather than waiting for it to finish -- a disconnect must stay
        prompt. A caller that needs an utterance to finish first should
        ``join`` its handle before calling this.

        Holds ``_worker_lock`` across the whole stop/sentinel/join sequence
        (the same lock :meth:`_enqueue` takes) -- see :class:`Speaker`'s
        docstring for why that's what keeps a racing ``say()`` from ever
        landing on a worker that's already seen this sentinel, and two
        concurrent ``close()`` calls from both trying to join/clear the same
        worker.
        """
        with self._worker_lock:
            self.stop()
            worker = self._worker
            if worker is not None:
                self._queue.put(None)  # sentinel: ask the worker to exit
                worker.join()
                if self._worker is worker:
                    self._worker = None
            self._opened = False
            self._voices.clear()

    def __enter__(self) -> Speaker:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
