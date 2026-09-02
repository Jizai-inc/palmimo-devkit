# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""NameMatcher — deciding whether a transcript called the robot by name.

Comparing a transcript to a name by spelling does not work for a name that is
not a word of the transcription language. Asked to write Japanese, a
transcriber renders "Palmimo" as whatever Latin-script approximation it
reached for -- measured on real calls, one per utterance and rarely twice the
same::

    Parmimo  Parumimo  Par mi mo  Paramimo  Pormimo  Farmimo  Varumimo  frmimo

Every one of those is the same sounds. So the comparison is done on sounds
rather than letters: both sides are folded to a **skeleton** -- kana
transliterated to romaji, long vowels and doubled consonants dropped, and the
consonant families a Latin rendering actually confuses (l/r, and b/v/f/h/p)
collapsed to one member each. The eight spellings above fold to skeletons at
or within a step of ``parmimo`` / ``parumimo`` -- close enough to the name's
own skeleton to clear the threshold below, not identical to it. What survives
is scored by :class:`difflib.SequenceMatcher`, because even the skeleton
varies at the edges.

Its ``ratio()`` is ``2M/T``: M the characters covered by matching runs, T the
two lengths added. Note it rewards *contiguous* runs rather than counting
shared characters, which is what keeps a long shared prefix from scoring like
a match -- the name "Harumi-san" folds to ``parumisan`` and shares six
characters with ``parumimo``, yet scores 0.71, below the threshold, because
nothing after the prefix lines up. (This is Ratcliff/Obershelp, not
Levenshtein; the two rank these cases differently, and the thresholds below
were measured against this one.)

The threshold matters and is not a matter of taste: on 80 recorded utterances
(40 calls, and 40 of ordinary speech that deliberately included words sharing
the name's sounds -- paama, harumi, marumi, paruma, haamonika, pantomaimu),
0.8 caught 39 of the 40 calls with no false accept, while 0.75 caught the same
39 and fired 7 times on ordinary speech. :data:`DEFAULT_THRESHOLD` is that
measurement, not a guess.

That "no false accept" measurement attacked the FULL name's sounds. It does
not cover two known exposures. First, the short form's four-character
skeleton ("mimo") is contained verbatim inside ordinary words -- the flower
name "mimosa", the Japanese word for "identity" (mimoto), and "yomi" plus
"mo" landing together across a word boundary -- so a transcript containing
one of those can score a perfect match on the short form alone. Second, a
lone name-like word shorter than the full name is compared against the whole
skeleton rather than a window of it, and can still clear the threshold on its
own -- a bare "Harumi" scores this way. Both are accepted for now rather than
fixed, because fixing either needs new recordings and a new measurement, the
same way this threshold was built; they are pinned by strict-xfail tests in
the test file so a future change to either fires visibly rather than
silently.

Both the consonant collapses above and the default threshold were measured
against recordings of ONE name: "Palmimo". Neither transfers to an arbitrary
name -- a different name has different confusable sounds and a different
separation point between "a call" and "ordinary speech that happens to share
sounds with it" -- so :class:`NameMatcher` does not accept a ``names``
argument; it listens for :data:`PALMIMO_NAMES` only. The threshold stays
overridable (:data:`DEFAULT_THRESHOLD` is a default, not a fixed value), but
its default is the measurement above, not a guess. Matching a different name
would need its own recordings and its own measurement, the same way this one
was built.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass


#: Similarity at or above which a window of the transcript counts as the name.
#: See the module docstring for the measurement behind the value.
DEFAULT_THRESHOLD = 0.8

#: The fixed set of ways the robot is called: the full name and the short
#: form. Compared by sound (see the module docstring), so listing more
#: spellings of the SAME sounds would add nothing -- these two are a genuinely
#: different way of being called, not variant spellings of one pronunciation.
#: The first element is the canonical display name.
PALMIMO_NAMES: tuple[str, ...] = ("パルミーモ", "ミーモ")

#: Hypotheses longer than this are truncated before matching. The comparison
#: cost grows with transcript length times the square of the name length
#: (linear in the transcript, not quadratic in it), and a wake word is not
#: buried in an essay -- this cap keeps a runaway transcript bounded.
MAX_TRANSCRIPT_CHARS = 400

_KATAKANA_START, _KATAKANA_END, _HIRAGANA_START = 0x30A1, 0x30F6, 0x3041

# Two-kana sequences must be looked up before the small-kana skip below.
# Otherwise a digraph loses its trailing small kana and degrades to the bare
# syllable -- cha becomes chi, so every name ending in -chan stops matching.
_DIGRAPHS = {
    "きゃ": "kya",
    "きゅ": "kyu",
    "きょ": "kyo",
    "しゃ": "sha",
    "しゅ": "shu",
    "しょ": "sho",
    "ちゃ": "cha",
    "ちゅ": "chu",
    "ちょ": "cho",
    "にゃ": "nya",
    "にゅ": "nyu",
    "にょ": "nyo",
    "ひゃ": "hya",
    "ひゅ": "hyu",
    "ひょ": "hyo",
    "みゃ": "mya",
    "みゅ": "myu",
    "みょ": "myo",
    "りゃ": "rya",
    "りゅ": "ryu",
    "りょ": "ryo",
    "ぎゃ": "gya",
    "ぎゅ": "gyu",
    "ぎょ": "gyo",
    "じゃ": "ja",
    "じゅ": "ju",
    "じょ": "jo",
    "びゃ": "bya",
    "びゅ": "byu",
    "びょ": "byo",
    "ぴゃ": "pya",
    "ぴゅ": "pyu",
    "ぴょ": "pyo",
}

_MONOGRAPHS = {
    "あ": "a",
    "い": "i",
    "う": "u",
    "え": "e",
    "お": "o",
    "か": "ka",
    "き": "ki",
    "く": "ku",
    "け": "ke",
    "こ": "ko",
    "が": "ga",
    "ぎ": "gi",
    "ぐ": "gu",
    "げ": "ge",
    "ご": "go",
    "さ": "sa",
    "し": "shi",
    "す": "su",
    "せ": "se",
    "そ": "so",
    "ざ": "za",
    "じ": "ji",
    "ず": "zu",
    "ぜ": "ze",
    "ぞ": "zo",
    "た": "ta",
    "ち": "chi",
    "つ": "tsu",
    "て": "te",
    "と": "to",
    "だ": "da",
    "ぢ": "ji",
    "づ": "zu",
    "で": "de",
    "ど": "do",
    "な": "na",
    "に": "ni",
    "ぬ": "nu",
    "ね": "ne",
    "の": "no",
    "は": "ha",
    "ひ": "hi",
    "ふ": "fu",
    "へ": "he",
    "ほ": "ho",
    "ば": "ba",
    "び": "bi",
    "ぶ": "bu",
    "べ": "be",
    "ぼ": "bo",
    "ぱ": "pa",
    "ぴ": "pi",
    "ぷ": "pu",
    "ぺ": "pe",
    "ぽ": "po",
    "ま": "ma",
    "み": "mi",
    "む": "mu",
    "め": "me",
    "も": "mo",
    "や": "ya",
    "ゆ": "yu",
    "よ": "yo",
    "ら": "ra",
    "り": "ri",
    "る": "ru",
    "れ": "re",
    "ろ": "ro",
    "わ": "wa",
    "ゐ": "i",
    "ゑ": "e",
    "を": "o",
    "ん": "n",
}

# Dropped outright: small kana (already consumed by _DIGRAPHS where they matter),
# the long-vowel mark, and punctuation/whitespace a transcriber sprinkles in.
_SKIPPED = re.compile(r"[ぁぃぅぇぉっゃゅょゎーｰ・~〜\s、。，．!?！？.,\-_'\"()（）「」]")

# The confusions a Latin-script rendering of a Japanese name actually produces.
# Applied after transliteration, one character in and one out, so the index map
# built alongside the skeleton stays aligned.
_CONSONANT_FOLD = str.maketrans({"l": "r", "v": "p", "f": "p", "h": "p", "b": "p"})


@dataclass(frozen=True)
class NameMatch:
    """A name detected in a transcript.

    Attributes:
        command: The transcript text that FOLLOWED the name, edge-trimmed. Empty
            when the name was the whole utterance.
        score: Similarity of the best-matching window, in ``[0, 1]``.
        matched: The entry of :data:`PALMIMO_NAMES` whose skeleton scored
            best. Diagnostic only -- it attributes sounds, not which name the
            speaker meant: an exact substring hit on the short form can
            out-score a fuzzy hit on the full name (e.g. "Parmimo dance"
            reports ``matched=PALMIMO_NAMES[1]``, the short form, not the
            full name it more closely resembles).
    """

    command: str
    score: float
    matched: str


def name_skeleton(text: str) -> str:
    """Fold *text* to the sound skeleton used for comparison.

    Exposed because it is the thing to print when a call was not recognized:
    seeing that a harmonica ("haamonika") reduces to ``paamonika`` while the
    name reduces to ``parumimo`` explains a decision that is otherwise opaque.
    """
    return _skeleton_with_index_map(unicodedata.normalize("NFKC", text))[0]


def _skeleton_with_index_map(nfkc: str) -> tuple[str, list[int]]:
    """Build the skeleton alongside a map from each of its characters back to *nfkc*.

    The map is what lets a match found in skeleton space be reported as an
    offset into real text, so the command following the name survives with its
    spacing intact.
    """
    folded: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(nfkc):
        code = ord(char)
        if _KATAKANA_START <= code <= _KATAKANA_END:
            char = chr(code - _KATAKANA_START + _HIRAGANA_START)
        for piece in char.casefold():  # casefold can expand one char into several
            folded.append(piece)
            origin.append(index)

    out: list[str] = []
    out_origin: list[int] = []
    i = 0
    while i < len(folded):
        pair = "".join(folded[i : i + 2])
        if len(pair) == 2 and pair in _DIGRAPHS:
            romaji, width = _DIGRAPHS[pair], 2
        elif folded[i] in _MONOGRAPHS:
            romaji, width = _MONOGRAPHS[folded[i]], 1
        elif _SKIPPED.fullmatch(folded[i]):
            i += 1
            continue
        else:
            romaji, width = folded[i], 1
        for piece in romaji:
            out.append(piece)
            out_origin.append(origin[i])
        i += width
    return "".join(out).translate(_CONSONANT_FOLD), out_origin


class NameMatcher:
    """Finds the robot's name -- "Palmimo" -- in a transcript, by sound rather than spelling.

    Args:
        threshold: Similarity at or above which a window of the transcript
            counts as the name. Defaults to :data:`DEFAULT_THRESHOLD`, the
            point measured to separate a real call from ordinary speech that
            happens to share sounds with the name (0.75 fired 7 times on the
            recorded corpus of ordinary speech; see the module docstring).
            Lowering it reintroduces those false accepts; raising it trades
            away calls the matcher currently catches. Overriding it is
            supported because it is a tuning knob, unlike the name itself:
            the names are fixed to :data:`PALMIMO_NAMES`, with no ``names``
            argument, because the consonant collapses (see the module
            docstring) were measured against recordings of this one name and
            do not transfer to an arbitrary one. A matcher for a different
            name would need its own recordings and its own measurement --
            that is deliberately not something this API offers.

    Raises:
        ValueError: If *threshold* is outside ``[0, 1]``.
    """

    def __init__(self, *, threshold: float = DEFAULT_THRESHOLD) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be within [0, 1]; got {threshold!r}.")
        self._targets = [(name, name_skeleton(name)) for name in PALMIMO_NAMES]
        self._threshold = threshold

    def match(self, transcript: str) -> NameMatch | None:
        """Return the best match in *transcript*, or ``None`` if the name was not heard."""
        nfkc = unicodedata.normalize("NFKC", transcript)[:MAX_TRANSCRIPT_CHARS]
        skeleton, origin = _skeleton_with_index_map(nfkc)
        if not skeleton:
            return None

        best_score, best_end, best_name = 0.0, 0, ""
        for name, target in self._targets:
            matcher = difflib.SequenceMatcher(None, target)
            span = len(target)
            # Windows LONGER than the target absorb an inserted mora and cost
            # nothing measurable. A shorter one is not offered: a window of
            # span-1 lets a prefix of an unrelated word score as the name:
            # "Harumi-san" folds to "parumisan", whose first 7 characters sit
            # close enough to "parumimo" to cross any threshold that still
            # catches real calls. That single window was the whole difference
            # between 0 and 5 false accepts on the recorded speech. The bound
            # is `- span + 1`, not `+ 2`: `start` in `range(n)` runs through
            # `n - 1`, so `- span + 1` lets the last `start` still be
            # `len(skeleton) - span` -- the last position that offers a
            # FULL-WIDTH window. One more would only ever produce a window
            # shorter than `span` (Python slicing truncates at the string's
            # end rather than raising), which is exactly the truncated,
            # shorter-than-target window the paragraph above rules out.
            for start in range(max(1, len(skeleton) - span + 1)):
                for width in (span, span + 1, span + 2):
                    window = skeleton[start : start + width]
                    if not window:
                        continue
                    matcher.set_seq2(window)
                    score = matcher.ratio()
                    if score > best_score:
                        best_score, best_end, best_name = score, start + len(window), name

        if best_score < self._threshold:
            return None
        cut = origin[best_end] if best_end < len(origin) else len(nfkc)
        return NameMatch(command=nfkc[cut:].strip(_EDGE_TRIM), score=round(best_score, 3), matched=best_name)


#: Trimmed from the edges of a returned command; inner spacing is left alone so
#: Latin word boundaries the caller may need survive.
_EDGE_TRIM = "、。，．!?！？.,: \t\n\r\f\v"


__all__ = ["DEFAULT_THRESHOLD", "PALMIMO_NAMES", "NameMatch", "NameMatcher", "name_skeleton"]
