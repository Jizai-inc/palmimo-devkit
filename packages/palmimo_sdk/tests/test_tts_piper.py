"""PiperEngine tests -- driven through the ``voice_loader`` seam (a fake
``PiperVoice.load``/``synthesize``), so no real ONNX inference is touched.

Voice-model *resolution* stays real: piper-plus's own ``find_voice`` runs
against dummy model files that ``_touch_voice_model`` drops under
``tmp_path``, so preflight/load-time resolution is exercised genuinely while
loading remains intercepted. First-run downloads are driven through a fake
``download_atomic`` that writes the same dummy files, so the catalogue lookup
and the resulting on-disk layout are real while the network is not.
"""

from pathlib import Path
from typing import Any

import pytest

from palmimo_sdk.download import default_model_dir
from palmimo_sdk.io.tts import PiperEngine
from palmimo_sdk.io.tts.piper import _probe_failure_hint, ensure_piper_voice


#: Two piper-plus catalogue voices whose configs are both named config.json --
#: the pair that used to overwrite each other in a shared directory.
CATALOGUE_JA = "ja_JP-tsukuyomi-chan-medium"
CATALOGUE_EN = "ja_JP-css10-6lang-medium"

#: A voice from piper's upstream catalogue rather than piper-plus's own. Only
#: these can be shadowed by a voices.json on disk: get_voices() merges
#: PIPER_PLUS_VOICES in last, so a file cannot redefine the two above.
UPSTREAM_VOICE = "ca_ES-upc_ona-medium"


def _touch_voice_model(data_dir: Path, name: str) -> None:
    """Create dummy files satisfying piper's standard voice-file naming
    convention, so ``piper.download.find_voice`` resolves ``name`` locally
    without any network access or real model content."""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / f"{name}.onnx").write_bytes(b"")
    (data_dir / f"{name}.onnx.json").write_text("{}")


class _FakeDownloads:
    """Fakes :func:`palmimo_sdk.download.download_atomic`: records each call
    and writes the URL it was asked to fetch into the destination file, so a
    test can tell which voice's files ended up where."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []
        self.error: Exception | None = None

    def __call__(self, url: str, dest: str | Path, *, timeout: float = 30.0, sha256: str | None = None) -> None:
        if self.error is not None:
            raise self.error
        path = Path(dest)
        self.calls.append((url, path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(url)


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> _FakeDownloads:
    import palmimo_sdk.io.tts.piper as piper_module

    fake = _FakeDownloads()
    monkeypatch.setattr(piper_module, "download_atomic", fake)
    return fake


class _FakeVoice:
    """Stands in for a loaded ``piper.PiperVoice`` — records ``synthesize``
    calls (including the ``length_scale``/``volume`` kwargs PiperEngine's
    wrapper passes through) instead of running real ONNX inference."""

    def __init__(self, model: str, loader: "_FakeVoiceLoader") -> None:
        self.model = model
        self._loader = loader

    def synthesize(self, text: str, wav_file: Any, **kwargs: Any) -> None:
        wav_file.setframerate(22050)
        wav_file.setsampwidth(2)
        wav_file.setnchannels(1)
        if self.model in self._loader.raise_on_synth:
            raise self._loader.raise_on_synth[self.model]
        self._loader.synth_calls.append((self.model, text, kwargs))
        wav_file.writeframes(b"\x00\x00")


class _FakeVoiceLoader:
    """Fakes :func:`palmimo_sdk.io.tts.piper._default_voice_loader`."""

    def __init__(self) -> None:
        self.load_calls: list[tuple[Path, Path]] = []
        self.synth_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.raise_on_load: dict[str, BaseException] = {}
        self.raise_on_synth: dict[str, BaseException] = {}

    def __call__(self, onnx_path: Path, config_path: Path) -> _FakeVoice:
        model = onnx_path.stem
        self.load_calls.append((onnx_path, config_path))
        if model in self.raise_on_load:
            raise self.raise_on_load[model]
        return _FakeVoice(model, self)


# ----------------------------------------------------------------------
# failure_hint
# ----------------------------------------------------------------------


def test_failure_hint_empty_for_unrecognized_error() -> None:
    engine = PiperEngine(model_en="a_model")
    assert engine.failure_hint("some other error", "en") == ""


def test_failure_hint_points_to_nltk_downloader_when_nltk_data_missing() -> None:
    engine = PiperEngine(model_en="a_model")
    stderr = (
        "LookupError: Resource averaged_perceptron_tagger_eng not found.\nSearched in:\n  - '/home/user/nltk_data'\n"
    )
    hint = engine.failure_hint(stderr, "en")
    assert "uv run python -m nltk.downloader averaged_perceptron_tagger_eng cmudict" in hint


def test_failure_hint_points_to_download_model_when_voice_not_found() -> None:
    engine = PiperEngine(model_en="a_model")
    hint = engine.failure_hint("piper.download.VoiceNotFoundError: a_model", "en")
    assert "uv run piper --download-model a_model" in hint


def test_failure_hint_download_dir_is_the_voices_own_directory() -> None:
    engine = PiperEngine(model_en="a_model", data_dir="/models")
    hint = engine.failure_hint("piper.download.VoiceNotFoundError: a_model", "en")
    assert f"--download-dir {Path('/models') / 'a_model'}" in hint


def test_failure_hint_download_dir_is_the_shared_cache_when_data_dir_unset() -> None:
    engine = PiperEngine(model_en="a_model")
    hint = engine.failure_hint("piper.download.VoiceNotFoundError: a_model", "en")
    assert f"--download-dir {default_model_dir() / 'piper' / 'a_model'}" in hint


def test_failure_hint_uses_the_model_for_the_failing_lang() -> None:
    engine = PiperEngine(model_en="en_model", model_ja="ja_model")
    hint = engine.failure_hint("piper.download.VoiceNotFoundError: ja_model", "ja")
    assert "uv run piper --download-model ja_model" in hint


def test_probe_failure_hint_helper_matches_engine_behavior() -> None:
    """The module-level helper PiperEngine.failure_hint delegates to."""
    assert _probe_failure_hint("some other error", "a_model", None) == ""
    assert "uv run piper --download-model a_model" in _probe_failure_hint(
        "piper.download.VoiceNotFoundError: a_model", "a_model", None
    )


# ----------------------------------------------------------------------
# where voice models live
# ----------------------------------------------------------------------


def test_voices_are_cached_outside_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, downloads: _FakeDownloads
) -> None:
    """The download lands in the shared model cache, never in the directory
    the process happens to run from -- following the documented setup used to
    drop ~38 MB per voice into the repository root."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)

    ensure_piper_voice(CATALOGUE_JA)

    assert list(tmp_path.glob("*.onnx")) == []
    assert {path.parent for _, path in downloads.calls} == {
        tmp_path / "cache" / "palmimo" / "models" / "piper" / CATALOGUE_JA
    }


def test_each_voice_gets_its_own_directory_so_their_configs_do_not_collide(
    tmp_path: Path, downloads: _FakeDownloads
) -> None:
    """Both piper-plus catalogue voices name their config ``config.json``.
    Downloaded into one shared directory, the second overwrote the first and
    the voices stopped pairing correctly; a directory per voice makes that
    structurally impossible."""
    ja_onnx, ja_config = ensure_piper_voice(CATALOGUE_JA, data_dir=str(tmp_path))
    en_onnx, en_config = ensure_piper_voice(CATALOGUE_EN, data_dir=str(tmp_path))

    assert ja_config != en_config
    assert ja_config.parent == ja_onnx.parent == tmp_path / CATALOGUE_JA
    assert en_config.parent == en_onnx.parent == tmp_path / CATALOGUE_EN
    # Each config was fetched from its own voice's repository.
    assert "tsukuyomi" in ja_config.read_text()
    assert "css10" in en_config.read_text()


def test_the_flat_layout_in_the_root_is_not_searched(tmp_path: Path, downloads: _FakeDownloads) -> None:
    """piper's own layout puts every voice's files side by side in one
    directory, which is exactly what mispairs the shared config.json. Finding
    a voice there would reintroduce the bug for anyone who already has one, so
    only the per-voice directory counts."""
    _touch_voice_model(tmp_path, "en_X")
    engine = PiperEngine(model_en="en_X", data_dir=str(tmp_path))

    with pytest.raises(RuntimeError, match="en_X"):
        engine.preflight("en")


def test_the_current_directory_is_not_searched_for_voice_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolution used to fall back to the process's cwd, which is what made
    the download target the repository root in the first place."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _touch_voice_model(cwd, "en_X")
    monkeypatch.chdir(cwd)
    engine = PiperEngine(model_en="en_X", data_dir=str(tmp_path / "cache"))

    with pytest.raises(RuntimeError, match="en_X"):
        engine.preflight("en")


# ----------------------------------------------------------------------
# ensure_piper_voice / preflight
# ----------------------------------------------------------------------


def test_preflight_passes_when_model_is_downloaded(tmp_path: Path) -> None:
    _touch_voice_model(tmp_path / "en_X", "en_X")
    engine = PiperEngine(model_en="en_X", data_dir=str(tmp_path))
    engine.preflight("en")  # must not raise


def test_preflight_downloads_the_voice_when_it_is_missing(tmp_path: Path, downloads: _FakeDownloads) -> None:
    engine = PiperEngine(model_ja=CATALOGUE_JA, data_dir=str(tmp_path))

    engine.preflight("ja")

    fetched = sorted(path.name for _, path in downloads.calls)
    assert fetched == ["config.json", "tsukuyomi-chan-6lang-fp16.onnx"]


def test_preflight_does_not_download_a_voice_that_is_already_present(tmp_path: Path, downloads: _FakeDownloads) -> None:
    _touch_voice_model(tmp_path / "en_X", "en_X")
    engine = PiperEngine(model_en="en_X", data_dir=str(tmp_path))

    engine.preflight("en")

    assert downloads.calls == []


def test_preflight_raises_with_an_offline_hint_when_the_download_fails(
    tmp_path: Path, downloads: _FakeDownloads
) -> None:
    downloads.error = OSError("network is unreachable")
    engine = PiperEngine(model_ja=CATALOGUE_JA, data_dir=str(tmp_path))

    with pytest.raises(RuntimeError, match="network is unreachable") as exc_info:
        engine.preflight("ja")

    message = str(exc_info.value)
    assert "offline" in message
    assert f"uv run piper --download-model {CATALOGUE_JA}" in message


def test_preflight_raises_when_the_model_is_not_in_the_catalogue(tmp_path: Path, downloads: _FakeDownloads) -> None:
    """A name piper-plus has never heard of cannot be fetched, so this fails
    with the manual command instead of attempting a download."""
    engine = PiperEngine(model_en="not_a_real_model", data_dir=str(tmp_path))

    with pytest.raises(RuntimeError, match="not_a_real_model") as exc_info:
        engine.preflight("en")

    assert downloads.calls == []
    assert "uv run piper --download-model not_a_real_model" in str(exc_info.value)


def test_ensure_refuses_a_non_https_download_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, downloads: _FakeDownloads
) -> None:
    """``get_voices()`` prefers a ``voices.json`` found in the target directory
    over the catalogue shipped inside piper-plus, so the URLs it yields are not
    unconditionally trustworthy -- piper-plus refuses non-HTTPS ones in its own
    downloader and so does this."""
    import palmimo_sdk.io.tts.piper as piper_module

    monkeypatch.setattr(
        piper_module,
        "_voice_file_urls",
        lambda model, download_dir: {"a.onnx": f"file:///{tmp_path}/planted.onnx"},
    )

    with pytest.raises(RuntimeError, match="non-HTTPS"):
        ensure_piper_voice("a_model", data_dir=str(tmp_path))

    assert downloads.calls == []


def test_preflight_checks_the_model_for_the_requested_lang(tmp_path: Path, downloads: _FakeDownloads) -> None:
    _touch_voice_model(tmp_path / "ja_Y", "ja_Y")
    engine = PiperEngine(model_en="en_missing", model_ja="ja_Y", data_dir=str(tmp_path))
    engine.preflight("ja")  # ja_Y is present -- must not raise
    with pytest.raises(RuntimeError, match="en_missing"):
        engine.preflight("en")


def test_preflight_with_fetch_false_reports_a_missing_voice_without_downloading(
    tmp_path: Path, downloads: _FakeDownloads
) -> None:
    """Speaker asks this about the language it is not opening with: report,
    do not spend ~38 MB on a voice the caller may never ask to speak."""
    engine = PiperEngine(model_ja=CATALOGUE_JA, data_dir=str(tmp_path))

    with pytest.raises(RuntimeError, match=CATALOGUE_JA):
        engine.preflight("ja", fetch=False)

    assert downloads.calls == []


def test_preflight_with_fetch_false_passes_when_the_voice_is_present(tmp_path: Path, downloads: _FakeDownloads) -> None:
    _touch_voice_model(tmp_path / "en_X", "en_X")
    engine = PiperEngine(model_en="en_X", data_dir=str(tmp_path))

    engine.preflight("en", fetch=False)  # must not raise

    assert downloads.calls == []


def test_ensure_raises_instead_of_leaking_a_corrupt_catalogue(tmp_path: Path, downloads: _FakeDownloads) -> None:
    """`get_voices()` reads a voices.json found in the voice directory. A
    corrupt one used to raise JSONDecodeError straight through preflight,
    past Speaker.open()'s `except RuntimeError` and out of connect()."""
    voice_dir = tmp_path / CATALOGUE_JA
    voice_dir.mkdir(parents=True)
    (voice_dir / "voices.json").write_text("{ this is not json")

    with pytest.raises(RuntimeError, match="voice catalogue"):
        ensure_piper_voice(CATALOGUE_JA, data_dir=str(tmp_path))

    assert downloads.calls == []


def test_ensure_refuses_a_repository_name_piper_would_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, downloads: _FakeDownloads
) -> None:
    """`repo` is the one attacker-controllable component of a piper-plus URL,
    and piper-plus validates it before building the same URL."""
    import piper.download as piper_download

    tampered = dict(piper_download.PIPER_PLUS_VOICES[CATALOGUE_JA])
    tampered["repo"] = "owner/../../evil"
    monkeypatch.setattr(
        piper_download, "PIPER_PLUS_VOICES", {**piper_download.PIPER_PLUS_VOICES, CATALOGUE_JA: tampered}
    )

    with pytest.raises(RuntimeError, match="unusable repository"):
        ensure_piper_voice(CATALOGUE_JA, data_dir=str(tmp_path))

    assert downloads.calls == []


def test_preflight_is_a_noop_when_piper_plus_is_not_importable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When piper-plus isn't importable, there is no resolver to search with
    and no library to feed a model to, so preflight has nothing to do and
    defers to the real failure surfacing from load_voice() instead."""
    import palmimo_sdk.io.tts.piper as piper_module

    monkeypatch.setattr(piper_module, "_find_piper_find_voice", lambda: None)
    engine = PiperEngine(model_en="not_downloaded_model", data_dir=str(tmp_path))
    engine.preflight("en")  # must not raise -- nothing to check


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        pytest.param(CATALOGUE_JA, "[]", id="not-a-mapping"),
        pytest.param(UPSTREAM_VOICE, f'{{"{UPSTREAM_VOICE}": "oops"}}', id="entry-not-a-mapping"),
        pytest.param(UPSTREAM_VOICE, f'{{"{UPSTREAM_VOICE}": {{"files": null}}}}', id="files-not-iterable"),
        pytest.param(UPSTREAM_VOICE, f'{{"{UPSTREAM_VOICE}": {{"files": [{{"a": 1}}]}}}}', id="file-entry-not-a-name"),
    ],
)
def test_ensure_raises_on_a_structurally_wrong_catalogue(
    model: str, payload: str, tmp_path: Path, downloads: _FakeDownloads
) -> None:
    """A voices.json that parses but is shaped wrong breaks piper-plus (or our
    own field accesses) with AttributeError/TypeError -- and Speaker.open()
    degrades only on RuntimeError, so anything else takes connect() down with
    a raw traceback. Every field access has to sit inside the guard, not just
    the parse."""
    voice_dir = tmp_path / model
    voice_dir.mkdir(parents=True)
    (voice_dir / "voices.json").write_text(payload)

    with pytest.raises(RuntimeError, match="voice catalogue"):
        ensure_piper_voice(model, data_dir=str(tmp_path))

    assert downloads.calls == []


@pytest.mark.parametrize("model", ["", ".", "..", "../escaped", "sub/voice"])
def test_ensure_refuses_a_model_name_that_is_a_path(model: str, tmp_path: Path, downloads: _FakeDownloads) -> None:
    """The model name is joined onto the cache root and comes from whoever
    configured the engine, so an absolute path would discard the root and
    ``..`` would escape it."""
    with pytest.raises(RuntimeError, match="not a plain voice name"):
        ensure_piper_voice(model, data_dir=str(tmp_path))

    assert downloads.calls == []


# ----------------------------------------------------------------------
# load_voice
# ----------------------------------------------------------------------


def test_load_voice_raises_when_piper_plus_is_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import palmimo_sdk.io.tts.piper as piper_module

    monkeypatch.setattr(piper_module, "_find_piper_find_voice", lambda: None)
    engine = PiperEngine(model_en="en_X", data_dir=str(tmp_path), voice_loader=_FakeVoiceLoader())
    with pytest.raises(RuntimeError, match="piper-plus is not installed"):
        engine.load_voice("en")


def test_load_voice_never_downloads(tmp_path: Path, downloads: _FakeDownloads) -> None:
    """load_voice runs on Speaker's worker thread, mid-utterance: a ~38 MB
    transfer there would sit outside say_timeout_s, ignore a barge-in stop(),
    and hold up close(). Fetching is preflight's job."""
    loader = _FakeVoiceLoader()
    engine = PiperEngine(model_ja=CATALOGUE_JA, data_dir=str(tmp_path), voice_loader=loader)

    with pytest.raises(RuntimeError, match=CATALOGUE_JA) as exc_info:
        engine.load_voice("ja")

    assert downloads.calls == []
    assert loader.load_calls == []  # the voice loader must never have been called
    assert f"uv run piper --download-model {CATALOGUE_JA}" in str(exc_info.value)


def test_load_voice_resolves_the_correct_model_for_each_lang(tmp_path: Path) -> None:
    _touch_voice_model(tmp_path / "en_X", "en_X")
    _touch_voice_model(tmp_path / "ja_Y", "ja_Y")
    loader = _FakeVoiceLoader()
    engine = PiperEngine(model_en="en_X", model_ja="ja_Y", data_dir=str(tmp_path), voice_loader=loader)
    engine.load_voice("en")
    engine.load_voice("ja")
    assert [p[0].stem for p in loader.load_calls] == ["en_X", "ja_Y"]


def test_load_voice_wraps_the_loaded_voice_so_synthesize_carries_length_scale_and_volume(
    tmp_path: Path,
) -> None:
    _touch_voice_model(tmp_path / "en_X", "en_X")
    loader = _FakeVoiceLoader()
    engine = PiperEngine(model_en="en_X", data_dir=str(tmp_path), length_scale=1.5, volume=0.7, voice_loader=loader)
    voice = engine.load_voice("en")

    import wave

    wav_path = tmp_path / "out.wav"
    with wave.open(str(wav_path), "wb") as wav_file:
        voice.synthesize("hello", wav_file)

    assert loader.synth_calls == [("en_X", "hello", {"length_scale": 1.5, "volume": 0.7})]


def test_load_voice_propagates_a_load_failure_with_voice_not_found_hint(tmp_path: Path) -> None:
    """Preflight only checks that a file exists at the expected path — it
    can't catch a corrupted or truncated model placed there by hand, so the
    loader's own load-failure path is still the backstop for that case."""

    class VoiceNotFoundError(Exception):
        pass

    _touch_voice_model(tmp_path / CATALOGUE_JA, CATALOGUE_JA)
    loader = _FakeVoiceLoader()
    loader.raise_on_load[CATALOGUE_JA] = VoiceNotFoundError(CATALOGUE_JA)
    engine = PiperEngine(model_ja=CATALOGUE_JA, data_dir=str(tmp_path), voice_loader=loader)
    with pytest.raises(VoiceNotFoundError):
        engine.load_voice("ja")


def test_name_is_piper() -> None:
    assert PiperEngine().name == "piper"
