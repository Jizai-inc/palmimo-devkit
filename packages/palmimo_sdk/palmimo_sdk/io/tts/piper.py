# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""PiperEngine — the piper-plus (MIT) :class:`~palmimo_sdk.io.tts.base.TtsEngine`.

Owns everything specific to piper-plus: its ``en``/``ja`` model names, its
own ``find_voice`` on-disk resolution (including piper's file-naming quirks,
e.g. ``ja_JP-css10-6lang-medium`` resolving to ``css10-ja-6lang-fp16.onnx``),
and the ``PiperVoice.load`` / ``.synthesize(..., length_scale=..., volume=...)``
calls. piper-plus is imported lazily throughout, so importing this module
(and therefore ``palmimo_sdk``) stays dependency-free.

Voice models are fetched on first use into the shared model cache, one
directory per voice — see :func:`ensure_piper_voice`. Audio-output setup
lives in ``README.md``.
"""

from __future__ import annotations

import logging
import os
import re
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...download import default_model_dir, download_atomic
from .base import TtsEngine, TtsVoice


logger = logging.getLogger(__name__)

#: Subdirectory of the shared model cache holding piper's voices.
_VOICE_CACHE_SUBDIR = "piper"

#: What one voice-model file (~38 MB each) may spend connecting, and waiting
#: for any single read. It does NOT bound the transfer: a slow but live link
#: keeps Speaker.open() -- and so robot.connect() -- waiting for as long as the
#: download honestly takes, which is the trade for never aborting one that is
#: making progress. What it does bound is silence, so an unreachable network
#: costs this once per file rather than hanging connect() indefinitely.
_DOWNLOAD_TIMEOUT_S = 30.0

#: The characters piper-plus allows in a catalogue voice's HuggingFace repo
#: (its own ``_SAFE_REPO_RE``), restated because it is private to piper.
_SAFE_REPO = re.compile(r"[a-zA-Z0-9._\-/]+")


def _voice_root(data_dir: str | None) -> Path:
    """Return the directory holding one subdirectory per piper voice.

    ``data_dir`` names that root explicitly (``~`` is expanded); ``None`` — the
    default — resolves to ``default_model_dir() / "piper"``, so voices land in
    the same cache every other auto-downloaded model uses instead of in
    whatever directory the process was started from.
    """
    if data_dir is not None:
        return Path(os.path.expanduser(data_dir))
    return default_model_dir() / _VOICE_CACHE_SUBDIR


def _voice_dir(model: str, data_dir: str | None) -> Path:
    """Return the one directory *model*'s files are resolved from and
    downloaded into: ``<root>/<model>/``.

    A voice gets a directory to itself, and piper's own flat layout (every
    voice's files side by side in one directory) is deliberately not searched:
    piper-plus's catalogue voices all name their config ``config.json``, so
    voices sharing a directory silently mispair — the second one's config
    overwrites the first's, which is the failure this layout exists to make
    impossible. A voice placed by hand goes in its own directory too; the
    command in :func:`_download_model_hint` names it.

    *model* is joined onto the root, so it has to be a plain name;
    :func:`_check_model_name` is what enforces that, at the one entry point
    (:func:`ensure_piper_voice`), leaving this a pure path join that error
    messages can call without themselves raising.
    """
    return _voice_root(data_dir) / model


def _check_model_name(model: str) -> None:
    """Raise :class:`RuntimeError` unless *model* is usable as a directory name.

    A model name is a piper-plus catalogue key, and it comes from whoever
    configured the engine (e.g. an app's ``VOICE_NAME`` setting), not from
    this module. :func:`_voice_dir` joins it onto the cache root, where an
    absolute path would silently discard the root and ``..`` would escape it.
    """
    if model in ("", ".", "..") or model != Path(model).name:
        raise RuntimeError(
            f"piper voice model {model!r} is not a plain voice name; a model name is a piper-plus "
            f"catalogue key (e.g. 'ja_JP-css10-6lang-medium'), not a path"
        )


def _download_model_hint(model: str, data_dir: str | None) -> str:
    """Return the ``uv run piper --download-model`` command that fetches
    *model* by hand into the directory :func:`_voice_dir` resolves it from —
    the offline / manual counterpart of :func:`ensure_piper_voice`."""
    return f"uv run piper --download-model {model} --download-dir {_voice_dir(model, data_dir)}"


def _probe_failure_hint(error_text: str, model: str, data_dir: str | None) -> str:
    """Return an actionable, English suffix for a probe failure, or "" if the
    cause doesn't match a known, fixable dependency gap.

    Recognizes the two runtime-dependency gaps a real synthesis probe can't
    otherwise explain: missing NLTK data (English phonemization) and an
    undownloaded or corrupted voice model. The common undownloaded-model case
    is normally handled earlier by :meth:`PiperEngine.preflight` (which fetches
    the voice without ever loading it); this hint remains as a backstop for
    failures preflight can't see, such as a model file that exists on disk —
    placed there by hand — but fails to load.
    """
    if "nltk_data" in error_text:
        return (
            " Missing NLTK data for English phonemization; run: "
            "uv run python -m nltk.downloader averaged_perceptron_tagger_eng cmudict"
        )
    if "VoiceNotFoundError" in error_text or "No such file" in error_text:
        return f" Voice model not found; run: {_download_model_hint(model, data_dir)}"
    return ""


def _find_piper_find_voice() -> Callable[[str, Any], tuple[Path, Path]] | None:
    """Return piper-plus's own ``find_voice`` resolver, or ``None`` if the
    piper-plus library isn't importable.

    Imported lazily so ``import palmimo_sdk`` stays dependency-free. When
    piper-plus truly isn't installed, callers treat ``None`` as "nothing to
    preflight" and let the later, real load raise instead.
    """
    try:
        from piper.download import find_voice
    except ImportError:
        return None
    return find_voice


def _find_local_voice_model(model: str, data_dir: str | None) -> tuple[Path, Path] | None:
    """Return *model*'s local ``(onnx, config)`` paths, or ``None`` when
    nothing resolves (including when piper-plus isn't importable).

    Reuses piper-plus's own ``find_voice`` resolver so piper-plus's file-naming
    quirks — the on-disk file name doesn't always match the model key, e.g.
    ``ja_JP-css10-6lang-medium`` resolves to ``css10-ja-6lang-fp16.onnx`` — are
    handled identically to how piper itself resolves them, instead of a naive
    re-implementation that could disagree with its actual lookup.
    """
    find_voice = _find_piper_find_voice()
    if find_voice is None:
        return None
    try:
        return find_voice(model, [str(_voice_dir(model, data_dir))])
    except ValueError:
        return None


def _voice_file_urls(model: str, download_dir: Path) -> dict[str, str]:
    """Map each of *model*'s catalogue files to its download URL, or return
    ``{}`` when the catalogue doesn't carry *model* (e.g. a bare ``.onnx``
    path used as a model name).

    The catalogue and both URL formats are read out of piper-plus itself
    rather than restated here, so a voice that moves — or a repository layout
    that changes upstream — is followed by upgrading piper-plus. Only the
    ``.onnx`` and its ``.json`` config are wanted; the catalogue's other
    entries (``MODEL_CARD``) are not files a voice loads.

    ``repo`` is validated the way piper-plus validates it before building the
    same URL: ``get_voices`` prefers a ``voices.json`` found in *download_dir*
    over the ``voices.json`` shipped inside the library, so it is the one
    component here that a file on disk can dictate. It cannot reach the two
    voices this SDK defaults to — ``get_voices`` merges its hard-coded
    ``PIPER_PLUS_VOICES`` in last, so those entries always win — but it can
    reach any upstream catalogue voice a caller configures.

    Raises:
        RuntimeError: The catalogue could not be read or is malformed. Callers
            surface a missing voice as a RuntimeError either way, and
            Speaker.open() degrades only on that type — a raw
            ``JSONDecodeError`` from a corrupt ``voices.json`` would instead
            take down ``connect()`` with a traceback.
    """
    try:
        return _read_voice_file_urls(model, download_dir)
    except RuntimeError:
        raise  # already the shape callers expect, with its own message
    except Exception as exc:
        # Deliberately broad, and deliberately around the whole read rather
        # than the parse alone: this is the boundary where a third-party
        # library hands back a structure we do not control (get_voices prefers
        # a voices.json found in download_dir over its own), and where an
        # unpinned piper-plus could rename what is imported. A catalogue that
        # parses but is shaped wrong surfaces as AttributeError or TypeError
        # from any of the field accesses, not just the first, and an upstream
        # rename as ImportError -- none of them a RuntimeError, which is the
        # only type Speaker.open() degrades on, so anything else takes
        # connect() down with a raw traceback.
        raise RuntimeError(f"could not read piper-plus's voice catalogue for {model!r}: {exc}") from exc


def _read_voice_file_urls(model: str, download_dir: Path) -> dict[str, str]:
    """The body of :func:`_voice_file_urls`, split out so every field access
    on the catalogue sits inside that function's guard."""
    from piper.download import PIPER_PLUS_URL_FORMAT, URL_FORMAT, get_voices

    voice_info = get_voices(download_dir).get(model)
    if voice_info is None:
        return {}
    urls: dict[str, str] = {}
    for file_path in voice_info["files"]:
        name = Path(file_path).name
        if not name.endswith((".onnx", ".json")):
            continue
        if voice_info.get("source") == "piper-plus":
            repo = voice_info.get("repo", "")
            if not isinstance(repo, str) or not _SAFE_REPO.fullmatch(repo) or ".." in repo:
                raise RuntimeError(f"piper-plus's catalogue gives {model!r} an unusable repository: {repo!r}")
            urls[name] = PIPER_PLUS_URL_FORMAT.format(repo=repo, file=name)
        else:
            urls[name] = URL_FORMAT.format(file=file_path)
    return urls


def ensure_piper_voice(model: str, data_dir: str | None = None, *, fetch: bool = True) -> tuple[Path, Path]:
    """Resolve *model*'s ``(onnx, config)`` paths, downloading it on first use.

    The counterpart of :func:`~palmimo_sdk.audio.denoise.ensure_denoise_model`
    for voices: an already-present voice is resolved and returned untouched,
    and a missing one is fetched into its own directory under
    :func:`_voice_root` (~38 MB) via :func:`~palmimo_sdk.download.download_atomic`,
    so a killed or stalled download can never leave a truncated model behind
    for the next run to load.

    Presence is piper-plus's own ``find_voice`` answer — the files exist where
    it looks. The catalogue's ``size_bytes`` is deliberately not enforced on
    top of that: it disagrees with what the repositories actually serve
    (``ja_JP-tsukuyomi-chan-medium``'s ``config.json`` is 6901 bytes against
    the catalogue's 6279), so enforcing it would re-download a correct file on
    every run. A file that exists but does not load is diagnosed by
    :func:`_probe_failure_hint` instead.

    Args:
        model: A piper-plus voice-model name (a catalogue key).
        data_dir: Root holding one directory per voice; ``None`` uses the
            shared model cache (see :func:`_voice_root`).
        fetch: When ``False``, report whether the voice is ready without
            downloading anything — a missing voice raises instead.

    Returns:
        The resolved, existing ``(onnx_path, config_path)`` pair.

    Raises:
        RuntimeError: piper-plus isn't installed, *model* is not a plain voice
            name or isn't in the catalogue, the catalogue is unreadable or
            offered a URL that cannot be trusted, ``fetch=False`` and the voice
            is missing, or the download failed (e.g. no network). The message
            carries the manual ``--download-model`` command for an offline
            venue.
    """
    if _find_piper_find_voice() is None:
        raise RuntimeError("piper-plus is not installed; uv sync (or uv add palmimo-sdk[speech])")
    _check_model_name(model)
    resolved = _find_local_voice_model(model, data_dir)
    if resolved is not None:
        return resolved

    target = _voice_dir(model, data_dir)
    if not fetch:
        # Nothing downloads this later: only preflight fetches, and the caller
        # asked it not to. Say what to run, not what will happen.
        raise RuntimeError(
            f"piper voice model {model!r} is not downloaded yet; {_download_model_hint(model, data_dir)}"
        )
    urls = _voice_file_urls(model, target)
    if not urls:
        raise RuntimeError(
            f"piper voice model {model!r} is not downloaded and is not in piper-plus's catalogue, "
            f"so it cannot be fetched automatically; place its files in {target} or run: "
            f"{_download_model_hint(model, data_dir)}"
        )
    # Checked before the first fetch, so one bad entry cannot leave half a
    # voice on disk. piper-plus refuses these in its own downloader, and
    # get_voices() prefers a voices.json sitting in the target directory over
    # the catalogue shipped inside the library, so the URLs are not
    # unconditionally trustworthy input.
    for url in sorted(urls.values()):
        if not url.startswith("https://"):
            raise RuntimeError(f"refusing to fetch piper voice model {model!r} over a non-HTTPS URL: {url}")
    logger.info("downloading piper voice model %s to %s (first run only)", model, target)
    for name, url in sorted(urls.items()):
        try:
            download_atomic(url, target / name, timeout=_DOWNLOAD_TIMEOUT_S)
        except OSError as exc:
            raise RuntimeError(
                f"failed to download piper voice model {model!r} from {url} to {target}: {exc}. "
                f"In an offline environment, fetch it on a networked machine and copy it to "
                f"{target}, or run: {_download_model_hint(model, data_dir)}"
            ) from exc

    resolved = _find_local_voice_model(model, data_dir)
    if resolved is None:
        raise RuntimeError(
            f"piper voice model {model!r} was downloaded to {target}, but piper-plus does not "
            f"resolve a usable voice there; {_download_model_hint(model, data_dir)}"
        )
    return resolved


def _default_voice_loader(onnx_path: Path, config_path: Path) -> Any:
    """Load a piper-plus voice model. This is the ~1.3s expensive step
    (ONNX Runtime session creation + warmup) that :meth:`PiperEngine.load_voice`
    pays once per language, not once per utterance.
    """
    from piper import PiperVoice  # lazy: keeps `import palmimo_sdk` dependency-free

    return PiperVoice.load(onnx_path, config_path)


class _PiperTtsVoice:
    """Thin :class:`~palmimo_sdk.io.tts.base.TtsVoice` wrapper around a loaded
    piper-plus ``PiperVoice``, closing over the engine's ``length_scale`` /
    ``volume`` knobs so callers only ever pass ``(text, wav_file)``."""

    def __init__(self, voice: Any, length_scale: float, volume: float) -> None:
        self._voice = voice
        self._length_scale = length_scale
        self._volume = volume

    def synthesize(self, text: str, wav_file: wave.Wave_write) -> None:
        self._voice.synthesize(text, wav_file, length_scale=self._length_scale, volume=self._volume)


class PiperEngine(TtsEngine):
    """:class:`~palmimo_sdk.io.tts.base.TtsEngine` over piper-plus.

    ``model_en`` / ``model_ja`` are piper-plus voice-model names (catalogue
    keys, not paths — see :func:`_voice_dir`); ``data_dir`` is the root holding
    one directory per downloaded voice (``None`` → the shared model cache, see
    :func:`_voice_root`).
    ``length_scale`` is phoneme length (``>1`` slower,
    ``<1`` faster); ``volume`` is the 0.1-2.0 multiplier -- both are applied
    to every voice this engine loads. ``voice_loader`` is a test seam
    (defaults to piper-plus's real ``PiperVoice.load``; fakes return an
    object with ``.synthesize(text, wav_file, ...)``).
    """

    name = "piper"

    def __init__(
        self,
        *,
        model_en: str = "ja_JP-css10-6lang-medium",
        model_ja: str = "ja_JP-tsukuyomi-chan-medium",
        data_dir: str | None = None,
        length_scale: float = 1.0,
        volume: float = 1.0,
        voice_loader: Callable[[Path, Path], Any] | None = None,
    ) -> None:
        self._model_en = model_en
        self._model_ja = model_ja
        self._data_dir = data_dir
        self._length_scale = length_scale
        self._volume = volume
        self._voice_loader = voice_loader or _default_voice_loader

    def _model_for(self, lang: str) -> str:
        return self._model_ja if lang == "ja" else self._model_en

    def preflight(self, lang: str, *, fetch: bool = True) -> None:
        """Make sure *lang*'s model is on disk, fetching it on first use (see
        :func:`ensure_piper_voice`); with ``fetch=False``, only report whether
        it is already there.

        This is where the download belongs: it happens at
        :meth:`~palmimo_sdk.io.speaker.Speaker.open` — atomically, with a
        timeout, and raising a hint-carrying :class:`RuntimeError` when
        offline — rather than deep inside a synthesis library mid-utterance.
        When piper-plus isn't importable, there is nothing to fetch or check
        here (no resolver to call); the later :meth:`load_voice` raises
        instead, same as before this preflight existed.
        """
        if _find_piper_find_voice() is None:
            return
        ensure_piper_voice(self._model_for(lang), self._data_dir, fetch=fetch)

    def load_voice(self, lang: str) -> TtsVoice:
        """Resolve *lang*'s on-disk model files, load them via ``voice_loader``,
        and wrap the result so callers only see the :class:`TtsVoice`
        contract (``synthesize(text, wav_file)``).

        Never downloads (``fetch=False``): this runs on Speaker's worker
        thread, mid-utterance, where a ~38 MB transfer would sit outside
        ``say_timeout_s``, ignore a barge-in ``stop()``, and hold up
        ``close()``. Fetching is :meth:`preflight`'s job, at open time.

        Raises :class:`RuntimeError` if piper-plus isn't installed, or if
        *lang*'s model is not downloaded (with a remediation hint).
        """
        onnx_path, config_path = ensure_piper_voice(self._model_for(lang), self._data_dir, fetch=False)
        voice = self._voice_loader(onnx_path, config_path)
        return _PiperTtsVoice(voice, self._length_scale, self._volume)

    def failure_hint(self, error_text: str, lang: str) -> str:
        return _probe_failure_hint(error_text, self._model_for(lang), self._data_dir)
