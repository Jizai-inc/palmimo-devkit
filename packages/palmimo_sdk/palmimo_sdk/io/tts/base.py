# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""TtsEngine — the text-to-speech engine boundary.

:class:`~palmimo_sdk.io.speaker.Speaker` owns the concurrency machinery
(job queue, resident worker, barge-in, timeouts, voice eviction/reload,
playback subprocess) and is engine-agnostic; a :class:`TtsEngine`
implementation only knows how to check, load, and diagnose voices for one
underlying synthesis library. This mirrors :class:`~palmimo_sdk.io.base.ServoDriver`:
the facade depends on the abstraction, and a concrete backend (here
:class:`~palmimo_sdk.io.tts.piper.PiperEngine`) adapts a specific library to it.
"""

from __future__ import annotations

import wave
from abc import ABC, abstractmethod
from typing import Protocol


class TtsVoice(Protocol):
    """A loaded voice: the per-utterance synthesis unit.

    Returned by :meth:`TtsEngine.load_voice` and cached by
    :class:`~palmimo_sdk.io.speaker.Speaker` for the life of the language
    (see ``_CachedVoice``) -- :meth:`synthesize` is called once per
    utterance, never once per :class:`TtsEngine`.
    """

    def synthesize(self, text: str, wav_file: wave.Wave_write) -> None:
        """Synthesize *text* into *wav_file*, writing frames as it goes.

        Args:
            text (str): The utterance to speak.
            wav_file (wave.Wave_write): An open WAV writer; the implementation
                sets its format (channels/sample width/frame rate) before
                writing frames.
        """
        ...


class TtsEngine(ABC):
    """The engine boundary: knows how to check, load, and diagnose voices.

    A :class:`TtsEngine` implementation carries whatever is specific to its
    underlying synthesis library (model names, on-disk resolution, per-call
    knobs like speed/volume); :class:`~palmimo_sdk.io.speaker.Speaker` only
    ever calls :meth:`preflight`, :meth:`load_voice`, and :meth:`failure_hint`
    against a ``lang`` string, and never inspects the engine's own state.
    """

    #: Short, human-readable engine name (e.g. ``"piper"``), used in
    #: Speaker's log lines and error messages so a failure names the engine
    #: at fault without Speaker itself knowing which engine is attached.
    name: str

    @abstractmethod
    def preflight(self, lang: str, *, fetch: bool = True) -> None:
        """Make *lang*'s assets ready, or raise :class:`RuntimeError` with a
        remediation hint when they can't be.

        Called by :meth:`~palmimo_sdk.io.speaker.Speaker.open` before paying
        the expensive :meth:`load_voice` cost. This is the one place an engine
        may fetch a missing model, so a first-run download happens at a
        predictable moment — bounded, and reported with a clear fix when it
        fails — instead of being triggered implicitly deep inside a synthesis
        library mid-utterance. Must not load the model.

        ``fetch=False`` asks the same question without paying for the answer:
        report whether the assets are ready, downloading nothing. Speaker uses
        it for the language it is *not* opening with, so opening an
        English-only robot never pulls the Japanese voice down; that one stays
        a warning at open time and an error if something later asks to speak
        it. An engine with nothing to fetch may ignore the flag.

        This is the only method allowed to download. :meth:`load_voice` runs
        on Speaker's worker thread mid-utterance, where a transfer would sit
        outside the utterance timeout, ignore a barge-in, and hold up
        ``close()``.
        """

    @abstractmethod
    def load_voice(self, lang: str) -> TtsVoice:
        """Load and return *lang*'s voice.

        The expensive step (model load / inference-session creation) that
        :meth:`~palmimo_sdk.io.speaker.Speaker.open` and
        :meth:`~palmimo_sdk.io.speaker.Speaker._get_voice` each pay once per
        language, not once per utterance.
        """

    def failure_hint(self, error_text: str, lang: str) -> str:
        """Return an actionable, English suffix for a probe failure, or
        ``""`` if the cause doesn't match a known, fixable gap.

        Args:
            error_text (str): The failure, formatted as ``f"{type(exc).__name__}: {exc}"``.
            lang (str): The language the failing probe was synthesizing.

        The default implementation has no diagnostics to offer.
        """
        return ""
