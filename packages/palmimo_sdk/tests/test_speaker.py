"""Speaker resource tests — driven through the ``engine`` (a fake
:class:`~palmimo_sdk.io.tts.base.TtsEngine`) and ``player`` (fake playback
process) seams, so no audio hardware, real ONNX inference, or piper-plus
model resolution is ever touched. Engine-specific behavior (piper-plus model
resolution, preflight, failure hints) is covered by ``test_tts_piper.py``;
these tests only cover what ``Speaker`` itself owns: the queue, resident
worker, barge-in, timeouts, voice cache, and playback.
"""

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from palmimo_sdk.io import Speaker, SpeakerConfig
from palmimo_sdk.io.speaker import _REAP_TIMEOUT_S, SpeechHandle, _Job
from palmimo_sdk.io.tts import TtsEngine, TtsVoice


class _FakeProcess:
    """Fakes the process handle a :class:`~palmimo_sdk.io.speaker.Player`
    returns from ``start()``. ``block=True`` makes ``communicate()`` hang
    until ``terminate()``/``kill()`` is called (or *timeout* elapses,
    raising ``subprocess.TimeoutExpired``, matching real ``Popen``), so
    cancellation is testable without a real OS subprocess. ``terminate_releases
    =False`` records the terminate()/kill() request (``self.terminated``/
    ``self.killed``) WITHOUT unblocking ``communicate()`` -- lets a test drive
    a "slow to actually die" process, releasing it later via ``_released.set()``."""

    def __init__(
        self,
        returncode: int | None = 0,
        stderr: bytes = b"",
        block: bool = False,
        terminate_releases: bool = True,
    ) -> None:
        self.returncode: int | None = returncode
        self._stderr = stderr
        self._block = block
        self._terminate_releases = terminate_releases
        self.communicate_calls: list[float | None] = []
        self.terminated = False
        self.killed = False
        self._released = threading.Event()

    def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
        self.communicate_calls.append(timeout)
        if self._block and not self._released.is_set():
            import subprocess

            if not self._released.wait(timeout=timeout):
                raise subprocess.TimeoutExpired(cmd="fake-player", timeout=timeout or 0)
            if self.returncode is None:
                self.returncode = -15
        return b"", self._stderr

    def terminate(self) -> None:
        self.terminated = True
        if self._terminate_releases:
            self._released.set()

    def kill(self) -> None:
        self.killed = True
        self._released.set()  # a real SIGKILL can't be ignored, unlike terminate_releases=False


class _FakePlayer:
    """Fakes playback (``aplay``/``afplay``): each ``start()`` call yields
    the next configured :class:`_FakeProcess`, or raises ``raises`` (e.g. a
    missing player binary)."""

    def __init__(
        self,
        results: list[tuple[int | None, bytes]] | None = None,
        raises: BaseException | None = None,
        block: bool = False,
        terminate_releases: bool = True,
    ) -> None:
        self.calls: list[list[str]] = []
        self.processes: list[_FakeProcess] = []
        self._results = list(results) if results else None
        self._raises = raises
        self._block = block
        self._terminate_releases = terminate_releases

    def start(self, argv: list[str]) -> _FakeProcess:
        self.calls.append(argv)
        if self._raises is not None:
            raise self._raises
        returncode, stderr = self._results.pop(0) if self._results else (0, b"")
        proc = _FakeProcess(
            returncode=returncode, stderr=stderr, block=self._block, terminate_releases=self._terminate_releases
        )
        self.processes.append(proc)
        return proc


class _FakeVoice:
    """Stands in for a loaded :class:`~palmimo_sdk.io.tts.base.TtsVoice` —
    records ``synthesize`` calls instead of running real synthesis."""

    def __init__(self, model: str, engine: "FakeEngine") -> None:
        self.model = model
        self._engine = engine

    def synthesize(self, text: str, wav_file: Any) -> None:
        # Configure the WAV header before any failure path -- an unconfigured
        # wave.Wave_write raises its own ("# channels not specified") error
        # from close() during exception unwind, masking whatever this method
        # raises below.
        wav_file.setframerate(22050)
        wav_file.setsampwidth(2)
        wav_file.setnchannels(1)
        engine = self._engine
        # Tracks how many synthesize() calls overlap in wall time, so a test
        # can assert Speaker never lets two calls into voice.synthesize() at
        # once (a TtsVoice isn't documented re-entrant).
        with engine.concurrent_synth_lock:
            engine.active_synth += 1
            engine.max_concurrent_synth = max(engine.max_concurrent_synth, engine.active_synth)
        try:
            if self.model in engine.hang_models:
                time.sleep(engine.hang_seconds)
            if self.model in engine.raise_on_synth:
                raise engine.raise_on_synth[self.model]
            engine.synth_calls.append((self.model, text))
            wav_file.writeframes(b"\x00\x00")
        finally:
            with engine.concurrent_synth_lock:
                engine.active_synth -= 1


class FakeEngine(TtsEngine):
    """Fakes :class:`~palmimo_sdk.io.tts.base.TtsEngine` -- the seam Speaker
    tests use in place of a real :class:`~palmimo_sdk.io.tts.piper.PiperEngine`.

    ``missing_langs`` makes :meth:`preflight` raise for those languages (a
    stand-in for an undownloaded model); ``model_en``/``model_ja`` are just
    opaque labels used to distinguish loaded voices in assertions, with no
    on-disk resolution behind them.
    """

    name = "fake"

    def __init__(
        self,
        model_en: str = "en_X",
        model_ja: str = "ja_Y",
        missing_langs: frozenset[str] = frozenset(),
        hang_seconds: float = 0.3,
    ) -> None:
        self.model_en = model_en
        self.model_ja = model_ja
        self.missing_langs = set(missing_langs)
        #: (lang, fetch) of every preflight() call, so a test can pin which
        #: language Speaker.open() is willing to pay a download for.
        self.preflight_calls: list[tuple[str, bool]] = []
        self.load_calls: list[tuple[Path, Path]] = []
        self.synth_calls: list[tuple[str, str]] = []
        self.raise_on_load: dict[str, BaseException] = {}
        self.raise_on_synth: dict[str, BaseException] = {}
        self.hang_models: set[str] = set()
        self.hang_seconds = hang_seconds
        self.concurrent_synth_lock = threading.Lock()
        self.active_synth = 0
        self.max_concurrent_synth = 0

    def _model_for(self, lang: str) -> str:
        return self.model_ja if lang == "ja" else self.model_en

    def preflight(self, lang: str, *, fetch: bool = True) -> None:
        self.preflight_calls.append((lang, fetch))
        if lang in self.missing_langs:
            model = self._model_for(lang)
            raise RuntimeError(f"fake voice model {model!r} is not downloaded yet; run: fake-download {model}")

    def load_voice(self, lang: str) -> TtsVoice:
        model = self._model_for(lang)
        self.load_calls.append((Path(model), Path(model)))
        if model in self.raise_on_load:
            raise self.raise_on_load[model]
        return _FakeVoice(model, self)

    def failure_hint(self, error_text: str, lang: str) -> str:
        if "nltk_data" in error_text:
            return " Missing NLTK data; run: fake-nltk-hint"
        if "VoiceNotFoundError" in error_text or "No such file" in error_text:
            return f" Voice model not found; run: fake-download {self._model_for(lang)}"
        return ""


def test_say_loads_the_model_once_across_two_utterances() -> None:
    """Two say() calls synthesize twice but load the model once."""
    engine = FakeEngine(model_en="en_X", model_ja="ja_Y")
    spk = Speaker(SpeakerConfig(lang="en"), engine=engine, player=_FakePlayer())
    spk.say("hello").join()
    spk.say("again").join()
    assert len(engine.load_calls) == 1  # the expensive part happened once
    assert engine.synth_calls == [("en_X", "hello"), ("en_X", "again")]


def test_say_plays_the_synthesized_audio() -> None:
    engine = FakeEngine(model_en="en_X")
    player = _FakePlayer()
    spk = Speaker(SpeakerConfig(lang="en"), engine=engine, player=player)
    spk.say("hello").join()
    assert len(player.calls) == 1
    assert player.calls[0][0] in ("aplay", "afplay", "play", "ffplay")


def test_say_lang_override_loads_the_other_language_voice() -> None:
    engine = FakeEngine(model_en="en_X", model_ja="ja_Y")
    spk = Speaker(SpeakerConfig(lang="en"), engine=engine, player=_FakePlayer())
    spk.say("やあ", "ja").join()
    assert engine.synth_calls == [("ja_Y", "やあ")]


def test_say_bilingual_speaks_en_then_ja_loading_each_voice_once() -> None:
    engine = FakeEngine(model_en="en_X", model_ja="ja_Y")
    spk = Speaker(SpeakerConfig(bilingual_gap_s=0), engine=engine, player=_FakePlayer())
    spk.say_bilingual("hello", "こんにちは").join()
    spk.say_bilingual("hi again", "またね")  # second call reuses both cached voices
    spk.say_bilingual("hi again", "またね").join()
    assert engine.synth_calls[:2] == [("en_X", "hello"), ("ja_Y", "こんにちは")]
    assert len(engine.load_calls) == 2  # one load per language, however many utterances


def test_say_swallows_synthesis_failure_without_raising() -> None:
    engine = FakeEngine(model_en="en_X")
    engine.raise_on_synth["en_X"] = RuntimeError("synthesis blew up")
    spk = Speaker(SpeakerConfig(), engine=engine, player=_FakePlayer())
    spk.say("hi").join()  # a broken voice must not crash the caller's thread


def test_say_records_synthesis_failure_on_the_handle() -> None:
    """A failure isn't swallowed by say() itself — it's left on the handle's
    error, so a joining caller can distinguish silence from speech."""
    engine = FakeEngine(model_en="en_X")
    engine.raise_on_synth["en_X"] = RuntimeError("synthesis blew up")
    spk = Speaker(SpeakerConfig(), engine=engine, player=_FakePlayer())
    handle = spk.say("hi")
    handle.join()
    assert isinstance(handle.error, RuntimeError)


def test_say_records_playback_failure_on_the_handle() -> None:
    engine = FakeEngine(model_en="en_X")
    spk = Speaker(SpeakerConfig(), engine=engine, player=_FakePlayer(raises=FileNotFoundError("aplay")))
    handle = spk.say("hi")
    handle.join()
    assert isinstance(handle.error, RuntimeError)
    assert "no audio player available" in str(handle.error)


def test_say_restarts_voice_after_synthesis_failure_and_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """ "Crash/liveness": a failed synthesis evicts the cached voice, and the
    next say() transparently reloads it (paying the model-load cost again)
    instead of staying wedged."""
    engine = FakeEngine(model_en="en_X")
    engine.raise_on_synth["en_X"] = RuntimeError("synthesis blew up")
    spk = Speaker(SpeakerConfig(), engine=engine, player=_FakePlayer())
    with caplog.at_level("WARNING"):
        spk.say("hi").join()  # fails; voice is evicted
        del engine.raise_on_synth["en_X"]  # simulate recovery
        spk.say("hi again").join()  # succeeds after a transparent reload
    assert len(engine.load_calls) == 2  # reloaded once after the failure
    assert engine.synth_calls == [("en_X", "hi again")]
    assert any("en" in message and "reload" in message for message in caplog.messages)


def test_say_times_out_long_synthesis_and_evicts_the_voice() -> None:
    """say_timeout_s now bounds synthesis + playback for a single utterance.
    Synthesis can't be hard-killed (no cancellation hook), so a slow voice is
    abandoned rather than interrupted -- but the caller still gets its error
    back on time, and the voice is evicted for a clean reload."""
    engine = FakeEngine(model_en="en_X", hang_seconds=0.3)
    engine.hang_models.add("en_X")
    player = _FakePlayer()
    spk = Speaker(SpeakerConfig(say_timeout_s=0.05), engine=engine, player=player)
    handle = spk.say("hi")
    handle.join(timeout=2)
    assert isinstance(handle.error, TimeoutError)
    assert player.calls == []  # playback never starts for an abandoned synthesis
    # The voice is evicted, so the next say() reloads it instead of reusing a
    # possibly-wedged session.
    spk.say("hi again").join(timeout=2)
    assert len(engine.load_calls) == 2


def test_timed_out_synthesis_deletes_its_late_wav(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An abandoned synthesis that finishes after the timeout already raised
    must delete its own temp WAV -- nobody is left to play or unlink it."""
    import tempfile

    engine = FakeEngine(model_en="en_X", hang_seconds=0.3)
    engine.hang_models.add("en_X")
    wav_dir = tmp_path / "wavs"
    wav_dir.mkdir()
    real_mkstemp = tempfile.mkstemp
    monkeypatch.setattr("palmimo_sdk.io.speaker.tempfile.mkstemp", lambda **kw: real_mkstemp(dir=str(wav_dir), **kw))
    spk = Speaker(SpeakerConfig(say_timeout_s=0.05), engine=engine, player=_FakePlayer())
    handle = spk.say("hi")
    handle.join(timeout=2)
    assert isinstance(handle.error, TimeoutError)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and any(wav_dir.iterdir()):
        time.sleep(0.02)
    assert list(wav_dir.iterdir()) == []


def test_say_timeout_bounds_playback_too() -> None:
    engine = FakeEngine(model_en="en_X")
    player = _FakePlayer()
    spk = Speaker(SpeakerConfig(say_timeout_s=12.5), engine=engine, player=player)
    spk.say("hi").join()
    assert player.processes[0].communicate_calls[0] == pytest.approx(12.5, abs=0.5)  # bounds a device-level hang


def test_open_raises_when_default_preflight_fails() -> None:
    """open() surfaces the engine's own preflight RuntimeError unwrapped --
    Speaker adds no framing to a preflight failure."""
    engine = FakeEngine(model_en="not_downloaded_model", missing_langs=frozenset({"en"}))
    spk = Speaker(SpeakerConfig(), engine=engine, player=_FakePlayer())
    with pytest.raises(RuntimeError, match="not_downloaded_model") as exc_info:
        spk.open()
    assert engine.load_calls == []  # the voice loader must never have been called
    assert "fake-download not_downloaded_model" in str(exc_info.value)
    assert not spk.is_open


def test_open_raises_when_probe_synthesis_fails() -> None:
    engine = FakeEngine(model_en="en_X")
    engine.raise_on_synth["en_X"] = RuntimeError("boom")
    spk = Speaker(SpeakerConfig(), engine=engine, player=_FakePlayer())
    with pytest.raises(RuntimeError, match="probe synthesis failed"):
        spk.open()
    assert not spk.is_open


def test_open_is_idempotent_loads_once() -> None:
    engine = FakeEngine(model_en="en_X")
    spk = Speaker(SpeakerConfig(), engine=engine, player=_FakePlayer())
    spk.open()
    spk.open()
    assert spk.is_open
    assert len(engine.load_calls) == 1


def test_open_probes_with_real_synthesis_and_never_plays_audio() -> None:
    """The probe must exercise actual synthesis -- but it must never reach
    the playback subprocess."""
    engine = FakeEngine(model_en="en_X", model_ja="ja_Y")
    player = _FakePlayer()
    spk = Speaker(SpeakerConfig(lang="en"), engine=engine, player=player)
    spk.open()
    assert engine.load_calls[0][0].stem == "en_X"
    assert engine.synth_calls  # the probe text was really synthesized
    assert player.calls == []  # playback subprocess never invoked


def test_open_error_uses_engines_failure_hint_for_nltk_data(caplog: pytest.LogCaptureFixture) -> None:
    engine = FakeEngine(model_en="en_X")
    engine.raise_on_synth["en_X"] = LookupError(
        "Resource averaged_perceptron_tagger_eng not found.\nSearched in:\n  - '/home/user/nltk_data'\n"
    )
    spk = Speaker(SpeakerConfig(), engine=engine, player=_FakePlayer())
    with pytest.raises(RuntimeError, match="fake-nltk-hint"):
        spk.open()


def test_open_error_uses_engines_failure_hint_when_voice_fails_to_load() -> None:
    """Preflight only checks that a model is registered as available — it
    can't catch a load-time failure, so the engine's own failure_hint (driven
    off the probe's error text) is still the backstop for that case."""

    class VoiceNotFoundError(Exception):
        pass

    engine = FakeEngine(model_ja="ja_JP-tsukuyomi-chan-medium")
    engine.raise_on_load["ja_JP-tsukuyomi-chan-medium"] = VoiceNotFoundError("ja_JP-tsukuyomi-chan-medium")
    spk = Speaker(SpeakerConfig(lang="ja"), engine=engine, player=_FakePlayer())
    with pytest.raises(RuntimeError, match=r"fake-download ja_JP-tsukuyomi-chan-medium"):
        spk.open()


def test_open_warns_but_does_not_raise_when_only_non_default_lang_preflight_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """say(lang=...) / say_bilingual need the other language's voice too, but a
    single-language setup (only the default voice available) must keep
    working — the gap is surfaced as a warning, not an open() failure."""
    engine = FakeEngine(model_en="en_present", model_ja="ja_missing", missing_langs=frozenset({"ja"}))
    spk = Speaker(SpeakerConfig(lang="en"), engine=engine, player=_FakePlayer())
    with caplog.at_level("WARNING"):
        spk.open()
    assert spk.is_open
    warning_text = "\n".join(caplog.messages)
    assert "ja" in warning_text
    assert "say(lang=...)" in warning_text
    assert "say_bilingual" in warning_text
    assert "ja_missing" in warning_text


def test_open_only_offers_to_fetch_the_language_it_opens_with() -> None:
    """A real engine's preflight downloads, and it is the only thing that does.
    Opening an English-only robot must not pull the Japanese voice down
    (~38 MB, plus ~75 MB once loaded) for a language the caller may never ask
    to speak; the fetch=False check still reports it missing, and a later
    say(lang="ja") fails with the command to fetch it rather than downloading
    mid-utterance."""
    engine = FakeEngine(model_en="en_present", model_ja="ja_present")
    spk = Speaker(SpeakerConfig(lang="en"), engine=engine, player=_FakePlayer())

    spk.open()

    assert engine.preflight_calls == [("en", True), ("ja", False)]


def test_open_does_not_warn_when_both_langs_preflight_cleanly(caplog: pytest.LogCaptureFixture) -> None:
    engine = FakeEngine(model_en="en_present", model_ja="ja_present")
    spk = Speaker(SpeakerConfig(lang="en"), engine=engine, player=_FakePlayer())
    with caplog.at_level("WARNING"):
        spk.open()
    assert caplog.messages == []


def test_close_resets_open_state() -> None:
    engine = FakeEngine(model_en="en_X")
    spk = Speaker(SpeakerConfig(), engine=engine, player=_FakePlayer())
    spk.open()
    assert spk.is_open
    spk.close()
    assert not spk.is_open


def test_close_evicts_the_resident_voice_so_say_reloads_it() -> None:
    """close() must terminate the resident resource reliably: a say() after
    close() pays the model-load cost again instead of silently reusing state
    close() was supposed to tear down."""
    engine = FakeEngine(model_en="en_X")
    spk = Speaker(SpeakerConfig(), engine=engine, player=_FakePlayer())
    spk.open()
    assert len(engine.load_calls) == 1
    spk.close()
    spk.close()  # idempotent
    spk.say("hi").join()
    assert len(engine.load_calls) == 2


def test_say_restarts_worker_after_close() -> None:
    """The resident worker is shut down by close() (see Speaker.close()) and
    must come back for the next say() -- a Speaker used again after close()
    behaves exactly like a fresh one, not a permanently wedged one."""
    engine = FakeEngine(model_en="en_X")
    spk = Speaker(SpeakerConfig(), engine=engine, player=_FakePlayer())
    spk.say("hi").join()
    spk.close()
    handle = spk.say("hi again")
    handle.join(timeout=2)
    assert handle.error is None
    assert engine.synth_calls[-1] == ("en_X", "hi again")


# ----------------------------------------------------------------------
# stop() — barge-in
# ----------------------------------------------------------------------


def test_speaker_stop_is_a_noop_when_idle() -> None:
    engine = FakeEngine(model_en="en_X")
    spk = Speaker(SpeakerConfig(), engine=engine, player=_FakePlayer())
    spk.stop()  # nothing ever spoke; must not raise


def test_stop_during_synthesis_prevents_playback_from_ever_starting() -> None:
    """A stop() that lands while synthesis is still running (no cancellation
    hook -- see _synthesize_with_timeout) must be honored right after: the
    not-yet-started playback is skipped instead of started."""
    engine = FakeEngine(model_en="en_X", hang_seconds=0.2)
    engine.hang_models.add("en_X")
    player = _FakePlayer()
    spk = Speaker(SpeakerConfig(say_timeout_s=5.0), engine=engine, player=player)
    handle = spk.say("hi")
    time.sleep(0.05)  # let synthesis actually start before stopping
    handle.stop()
    handle.join(timeout=2)
    assert player.calls == []  # playback never started


def test_stop_during_synthesis_releases_worker_promptly_for_next_say_on_a_different_voice() -> None:
    """A stop() landing mid-synthesis must free the WORKER for the next job
    well before the synthesis itself would finish -- _synthesize_with_timeout
    polls in slices instead of one blocking join precisely so this doesn't
    happen. Uses a *different* language for the second utterance: waiting
    when the next job is on the SAME (abandoned) voice is now a deliberate,
    separate behavior -- see
    test_stop_during_synthesis_then_new_utterance_never_overlaps_the_abandoned_synthesis."""
    engine = FakeEngine(model_en="en_X", model_ja="ja_Y", hang_seconds=2.0)
    engine.hang_models.add("en_X")
    player = _FakePlayer()
    spk = Speaker(SpeakerConfig(say_timeout_s=10.0), engine=engine, player=player)
    first = spk.say("hi")  # "en" -- hangs
    time.sleep(0.05)  # let first's synthesis actually start
    first.stop()

    start = time.monotonic()
    second = spk.say("hi again", "ja")  # different voice -- must not wait on "en"'s lock
    second.join(timeout=1.0)
    elapsed = time.monotonic() - start

    assert not second.is_alive()
    assert elapsed < 1.0  # far under first's 2s hang -- the worker wasn't stuck on it
    assert "en" in spk._voices  # a stop is not a synthesis failure -- no eviction


def test_stop_drains_queued_jobs_without_running_them() -> None:
    """stop() must not just interrupt the job in progress: jobs still
    waiting in the queue are drained too, each marked stopped and done, so a
    caller joining one of them isn't left waiting for the worker to reach it
    -- and it never actually synthesizes."""
    engine = FakeEngine(model_en="en_X", hang_seconds=1.0)
    engine.hang_models.add("en_X")
    spk = Speaker(SpeakerConfig(say_timeout_s=5.0), engine=engine, player=_FakePlayer())
    first = spk.say("first")  # occupies the worker (hung synthesis)
    time.sleep(0.05)  # let first's synthesis actually start
    second = spk.say("second")  # queued -- never reaches the worker
    third = spk.say("third")  # queued -- never reaches the worker

    spk.stop()
    second.join(timeout=1.0)
    third.join(timeout=1.0)

    assert second.stop_requested is True
    assert third.stop_requested is True
    assert not second.is_alive()
    assert not third.is_alive()
    assert ("en_X", "second") not in engine.synth_calls
    assert ("en_X", "third") not in engine.synth_calls
    first.join(timeout=2.0)  # let the abandoned first job's worker cycle settle


def test_speaker_stop_terminates_in_flight_playback_promptly() -> None:
    """A stop() must reach playback that's actually under way right now,
    terminating it instead of waiting the full (fake, slow) play out."""
    engine = FakeEngine(model_en="en_X")
    player = _FakePlayer(block=True)
    spk = Speaker(SpeakerConfig(say_timeout_s=10.0), engine=engine, player=player)
    handle = spk.say("hi")

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not player.processes:
        time.sleep(0.01)
    assert player.processes, "playback never started"

    start = time.monotonic()
    spk.stop()
    handle.join(timeout=2)
    assert time.monotonic() - start < 2  # terminated promptly, not left blocking
    assert player.processes[0].terminated is True


def test_speech_handle_stop_is_idempotent() -> None:
    engine = FakeEngine(model_en="en_X")
    player = _FakePlayer(block=True)
    spk = Speaker(SpeakerConfig(say_timeout_s=10.0), engine=engine, player=player)
    handle = spk.say("hi")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not player.processes:
        time.sleep(0.01)
    handle.stop()
    handle.stop()  # must not raise
    handle.join(timeout=2)


def test_speaker_stop_terminates_in_flight_playback_on_real_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default (no injected player) path uses a real subprocess; this is
    exercised against a real, slow OS process (``sleep``) so the default
    :class:`~palmimo_sdk.io.speaker._SubprocessPlayer` itself -- not just the
    fake -- is covered for prompt cancellation."""
    import palmimo_sdk.io.speaker as speaker_module

    engine = FakeEngine(model_en="en_X")
    monkeypatch.setattr(speaker_module, "_playback_argv_candidates", lambda wav_path: [["sleep", "5"]])
    spk = Speaker(SpeakerConfig(say_timeout_s=10.0), engine=engine)  # no player: real subprocess path
    handle = spk.say("hi")

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and spk._current_proc is None:  # wait for playback to actually start
        time.sleep(0.01)
    assert spk._current_proc is not None, "playback subprocess never started"

    start = time.monotonic()
    spk.stop()
    handle.join(timeout=2)
    assert time.monotonic() - start < 2  # terminated promptly, not after the full 5s sleep


def test_context_manager_opens_and_closes() -> None:
    engine = FakeEngine(model_en="en_X")
    spk = Speaker(SpeakerConfig(), engine=engine, player=_FakePlayer())
    with spk as s:
        assert s.is_open
    assert not spk.is_open


# ----------------------------------------------------------------------
# Review fixes: sentinel-swallow deadlock, stop-vs-dequeue race,
# probe/synthesis serialization
# ----------------------------------------------------------------------


def test_close_does_not_deadlock_when_stop_lands_after_the_close_sentinel_is_queued() -> None:
    """Regression for a deadlock: close() calls stop() (which terminates the
    in-flight job) then queues its exit sentinel; if another stop() call
    lands in between and drains that sentinel without re-queuing it, the
    worker never sees it, close()'s worker.join() never returns, and close()
    hangs forever. Even with close() now holding `_worker_lock` across its
    whole stop/sentinel/join sequence (see the worker-lifecycle-atomicity
    tests below), a plain external stop() -- which does NOT take that lock
    -- can still interleave and drain the sentinel mid-close.

    Uses a player whose terminate() is recorded but doesn't unblock playback
    yet (terminate_releases=False), so the test can force the exact
    interleaving: let close()'s own stop() request the terminate, wait until
    the sentinel is actually sitting in the queue (not just requested --
    gating only on the terminate flag would let this pass vacuously against
    an empty queue), call a second stop() while the job is still
    "terminating", then release playback and confirm close() still returns
    promptly instead of hanging."""
    engine = FakeEngine(model_en="en_X")
    player = _FakePlayer(block=True, terminate_releases=False)
    spk = Speaker(SpeakerConfig(say_timeout_s=10.0), engine=engine, player=player)
    spk.say("hi")  # occupies the worker in blocking playback

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not player.processes:
        time.sleep(0.01)
    assert player.processes, "playback never started"

    close_done = threading.Event()

    def _close() -> None:
        spk.close()
        close_done.set()

    closer = threading.Thread(target=_close, daemon=True)
    closer.start()

    # Wait for close()'s own stop() to have requested termination (recorded,
    # but not yet released -- see terminate_releases=False).
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not player.processes[0].terminated:
        time.sleep(0.01)
    assert player.processes[0].terminated

    # And for its exit sentinel to actually be enqueued -- not just about to
    # be -- so the drain below is guaranteed to see it.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and spk._queue.qsize() < 1:
        time.sleep(0.01)
    assert spk._queue.qsize() >= 1

    spk.stop()  # must re-queue the sentinel it drains, not swallow it
    player.processes[0]._released.set()  # let the in-flight playback actually finish now

    closer.join(timeout=2)
    assert close_done.is_set()  # close() must return, not hang forever


def test_run_job_honors_a_stop_that_lands_after_the_job_was_already_dequeued() -> None:
    """A job the worker has already popped off the queue is invisible to
    Speaker.stop()'s drain (it isn't in the queue anymore) and isn't yet
    `_current_handle` either -- so without the stop-epoch check, a stop()
    landing in that exact window would be silently missed and the job would
    speak in full. `_run_job` compares its stop-epoch against the job's
    recorded epoch before registering it as current."""
    engine = FakeEngine(model_en="en_X")
    player = _FakePlayer()
    spk = Speaker(SpeakerConfig(), engine=engine, player=player)

    handle = SpeechHandle(spk)
    job = _Job(handle=handle, utterances=[("hi", "en")], enqueued_at_stop_count=0)
    spk.stop()  # bumps _stop_count past the job's recorded epoch (0 -> 1)

    spk._run_job(job)

    assert player.calls == []
    assert engine.synth_calls == []
    assert handle.stop_requested is True
    assert handle.is_alive() is False


def test_speak_once_never_lets_two_synthesis_calls_overlap() -> None:
    """Concurrent _speak_once calls on the same cached voice -- e.g. the
    worker's own synthesis racing open()'s probe -- never call
    voice.synthesize() at the same time (a TtsVoice isn't documented
    re-entrant). Serialized by the voice's own lock (see _CachedVoice), not
    by Speaker._lock (which only guards the cache lookup itself)."""
    engine = FakeEngine(model_en="en_X", hang_seconds=0.15)
    engine.hang_models.add("en_X")
    spk = Speaker(SpeakerConfig(say_timeout_s=5.0), engine=engine, player=_FakePlayer())

    threads = [threading.Thread(target=spk._speak_once, args=(f"text{i}", "en")) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert engine.max_concurrent_synth == 1
    assert len(engine.synth_calls) == 3


def test_stop_during_synthesis_then_new_utterance_never_overlaps_the_abandoned_synthesis() -> None:
    """Regression: stop() abandons the synth thread (no cancellation hook)
    -- it keeps running voice.synthesize() on the cached voice, which is NOT
    evicted on a deliberate stop. Holding Speaker._lock across synthesis (the
    previous fix) can't help here: the abandoned thread doesn't hold that
    lock, so the very next utterance on the same voice could start
    synthesizing concurrently with it. Per-voice serialization (see
    _CachedVoice) closes this: the next call waits on the SAME voice's own
    lock, which the abandoned thread still holds."""
    engine = FakeEngine(model_en="en_X", hang_seconds=0.3)
    engine.hang_models.add("en_X")
    spk = Speaker(SpeakerConfig(say_timeout_s=5.0), engine=engine, player=_FakePlayer())

    handle = spk.say("first")
    time.sleep(0.05)  # let synthesis actually start
    handle.stop()  # abandons the synth thread -- still running, voice NOT evicted

    second = spk.say("second")  # same voice, same fake object -- must wait, not overlap
    second.join(timeout=2)

    assert engine.max_concurrent_synth == 1
    assert "en" in spk._voices  # not evicted by a deliberate stop
    assert second.error is None


# ----------------------------------------------------------------------
# Second review pass: worker-lifecycle atomicity, per-voice synthesis
# serialization, queued-handle stop, bounded reap, malformed returncode
# ----------------------------------------------------------------------


def test_say_racing_close_never_orphans_the_job() -> None:
    """A say() landing while close() is tearing down the worker must not be
    orphaned: _enqueue and close() share `_worker_lock` across their whole
    sequence (see Speaker's docstring), so say() either lands on the
    still-live worker before close() gets the lock, or waits for close() to
    finish and starts a fresh worker -- either way the handle finishes,
    never hangs waiting on a worker that already consumed its exit
    sentinel."""
    engine = FakeEngine(model_en="en_X")
    spk = Speaker(SpeakerConfig(say_timeout_s=5.0), engine=engine, player=_FakePlayer())
    spk.say("warmup").join(timeout=2)  # worker started and idle

    barrier = threading.Barrier(2)
    handle_box: list[SpeechHandle] = []

    def _close() -> None:
        barrier.wait(timeout=2)
        spk.close()

    def _say() -> None:
        barrier.wait(timeout=2)
        handle_box.append(spk.say("racer"))

    closer = threading.Thread(target=_close)
    sayer = threading.Thread(target=_say)
    closer.start()
    sayer.start()
    closer.join(timeout=2)
    sayer.join(timeout=2)

    assert not closer.is_alive()
    assert not sayer.is_alive()
    assert handle_box, "say() never returned a handle"
    handle_box[0].join(timeout=2)
    assert not handle_box[0].is_alive()  # never orphaned, whichever side of close() it landed on


def test_concurrent_close_calls_both_return_and_next_say_still_speaks() -> None:
    """Two close() calls racing each other must both return (not one hanging
    behind the other's worker.join(), and not each independently queuing a
    sentinel and colliding), and afterward a fresh say() must still work --
    a stale sentinel left behind by the race would otherwise wedge the next
    worker."""
    engine = FakeEngine(model_en="en_X")
    spk = Speaker(SpeakerConfig(say_timeout_s=5.0), engine=engine, player=_FakePlayer())
    spk.say("warmup").join(timeout=2)  # worker started and idle

    barrier = threading.Barrier(2)

    def _close() -> None:
        barrier.wait(timeout=2)
        spk.close()

    closers = [threading.Thread(target=_close) for _ in range(2)]
    for t in closers:
        t.start()
    for t in closers:
        t.join(timeout=2)
    for t in closers:
        assert not t.is_alive()  # neither close() call hung

    handle = spk.say("after both closes")
    handle.join(timeout=2)
    assert handle.error is None
    assert engine.synth_calls[-1] == ("en_X", "after both closes")


def test_stop_on_a_queued_handle_unblocks_join_immediately() -> None:
    """SpeechHandle.stop() on a handle that isn't the worker's current job
    (still queued behind another) must not leave join() blocked behind the
    job(s) ahead of it -- it marks itself done right away instead of relying
    on the worker to eventually dequeue and skip it."""
    engine = FakeEngine(model_en="en_X", hang_seconds=2.0)
    engine.hang_models.add("en_X")
    spk = Speaker(SpeakerConfig(say_timeout_s=5.0), engine=engine, player=_FakePlayer())
    first = spk.say("first")  # occupies the worker (hung synthesis)
    time.sleep(0.05)  # let first's synthesis actually start
    second = spk.say("second")  # queued behind first -- not yet current

    start = time.monotonic()
    second.stop()
    second.join(timeout=0.5)
    elapsed = time.monotonic() - start

    assert second.stop_requested is True
    assert not second.is_alive()
    assert elapsed < 0.5  # must not wait behind first's still-running job

    first.stop()  # cleanup: let the first job settle too
    first.join(timeout=2.0)


def test_run_playback_escalates_to_kill_when_terminate_is_ignored() -> None:
    """The stopped-before-registration reap path in _run_playback must not
    wedge the worker forever on a Player that ignores terminate() -- _reap()
    escalates to kill() after a bounded wait instead of an unbounded
    communicate()."""
    engine = FakeEngine(model_en="en_X")
    player = _FakePlayer(block=True, terminate_releases=False)
    spk = Speaker(SpeakerConfig(), engine=engine, player=player)
    handle = SpeechHandle(spk)
    handle.stop()  # stop_requested True before _run_playback ever starts the process

    done = threading.Event()

    def _run() -> None:
        spk._run_playback(["afplay", "x"], timeout=5.0, handle=handle)
        done.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=_REAP_TIMEOUT_S + 2)

    assert done.is_set()  # must not wedge the worker forever
    assert player.processes[0].killed is True  # terminate() alone wasn't enough


def test_run_playback_raises_when_player_returns_no_returncode() -> None:
    """A Player whose process reports no returncode after communicate() is a
    protocol violation, not a normal nonzero-exit failure -- must not be
    silently coerced into "success" by `None or 0 == 0`."""
    engine = FakeEngine(model_en="en_X")
    player = _FakePlayer(results=[(None, b"")])
    spk = Speaker(SpeakerConfig(), engine=engine, player=player)
    with pytest.raises(RuntimeError, match="returncode"):
        spk._run_playback(["afplay", "x"], timeout=1.0, handle=None)


def test_synthesize_to_tmp_wav_deletes_the_file_when_synthesize_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If voice.synthesize() raises, the mkstemp'd WAV path must not leak --
    nobody else is left to clean it up."""
    import tempfile

    engine = FakeEngine(model_en="en_X")
    engine.raise_on_synth["en_X"] = RuntimeError("boom")
    wav_dir = tmp_path / "wavs"
    wav_dir.mkdir()
    real_mkstemp = tempfile.mkstemp
    monkeypatch.setattr("palmimo_sdk.io.speaker.tempfile.mkstemp", lambda **kw: real_mkstemp(dir=str(wav_dir), **kw))
    spk = Speaker(SpeakerConfig(), engine=engine, player=_FakePlayer())
    spk.say("hi").join(timeout=2)
    assert list(wav_dir.iterdir()) == []


def test_default_engine_is_piper_engine() -> None:
    """No engine= means a fresh PiperEngine, so Speaker() keeps working for
    existing callers that never heard of TtsEngine."""
    from palmimo_sdk.io.tts import PiperEngine

    spk = Speaker()
    assert isinstance(spk._engine, PiperEngine)
    assert spk._engine.name == "piper"
