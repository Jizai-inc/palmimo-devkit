"""NameMatcher pins the sounds it accepts, and the ones it must keep refusing.

The spellings below are not invented. Every string in
``TRANSCRIBED_CALLS`` came back from ``gpt-4o-mini-transcribe`` while someone
called the robot into a ReSpeaker on real hardware, and every string in
``ORDINARY_SPEECH`` came from the same microphone in the same session with the
name deliberately not spoken. They are the reason the matcher exists, so they
are what it is tested against.
"""

from __future__ import annotations

import inspect

import pytest

from palmimo_sdk import PALMIMO_NAMES, NameMatcher, name_skeleton
from palmimo_sdk.name_match import DEFAULT_THRESHOLD


#: NameMatcher listens for exactly PALMIMO_NAMES; the corpus below tests
#: against that fixed set rather than a locally-defined one, so a drift
#: between the two would show up immediately.
NAMES = PALMIMO_NAMES

#: Actual transcripts of someone saying the name. One spelling per call and
#: rarely the same one twice -- which is exactly why spelling comparison failed.
TRANSCRIBED_CALLS = [
    "Paramimo",
    "Pormimo.",
    "harmimo",
    "Farmimo!",
    "Parmi mo",
    "Par mimó!",
    "Par mi mo.",
    "Parmiimo",
    "frmimo",
    "Varumimo",
    "Parumimo.",
    "Parunimo",
    "paruimo",
    "Faru ni mo.",
    "parumi mo.",
    "ワルミモ",
    "バルミーもー?",
    "ねえ、パルミモちゃん。",
    "パルミーもちゃん。",
    "パルミーモちゃん。",
    "まるみーもちゃん",
]

#: Actual transcripts from the same microphone with the name never spoken. The
#: second half was read deliberately: every one of them shares sounds with the
#: name, which is where a sound-based matcher is at risk of firing.
ORDINARY_SPEECH = [
    "今日は朝から風が強くて、洗濯物も外に干すか迷いました。",
    "昼ご飯は冷蔵庫の残りで済ませるつもりです。",
    "それも面白いし、これももらっておこうかな。",
    "もう少しだけ待ってもらえますか?",
    "パマをかけたはるみさんとまるみさんが来た、パルマ産のハム。",
    "ミルクを温めて飲んだモーターの音が",
    "うるさい。パラソル。",
    "丸々一日休んだ。",
    "ハーモニカを吹いた。",
    "パントマイムの練習をした。",
    "丸見えになっている。",
    "ぬるま湯でいい。",
    "バルコニーに出た。",
    "プリンターの紙がない。",
    "バルマさんのハムを買った。",
    "たるみさんとまるみさんが来た。",
    "マンモスの母型を見た。",
    # Pin the window-truncation regression: a sentence-final name-like word (or
    # a short word) at the transcript tail must not be offered as a
    # truncated, shorter-than-target window (see NameMatcher.match).
    "こんにちは、はるみ",
    "よろしくね、はるみ",
    "じゃがいも",
]


def test_public_contract_has_no_names_parameter() -> None:
    """Regression guard: name customization stays removed.

    NameMatcher's consonant collapses were measured against one name, so the
    constructor must never grow a ``names`` parameter back -- that would
    silently ship an unmeasured configuration. ``threshold`` is deliberately
    allowed: it is a tuning knob, not a per-name measurement.
    """
    assert list(inspect.signature(NameMatcher).parameters) == ["threshold"]


def test_palmimo_names_is_the_fixed_name_and_short_form() -> None:
    assert PALMIMO_NAMES == ("パルミーモ", "ミーモ")
    assert PALMIMO_NAMES[0] == "パルミーモ"  # the canonical display name


def test_skeleton_collapses_the_spellings_a_transcriber_produces() -> None:
    """The whole design in one assertion: different letters, same sounds."""
    assert name_skeleton("パルミーモ") == name_skeleton("Parumimo") == name_skeleton("Varumimo")
    assert name_skeleton("Palmimo") == name_skeleton("Farmimo") == name_skeleton("harmimo")


def test_skeleton_keeps_a_digraph_whole() -> None:
    """A small kana bends the syllable before it; it must not be dropped first.

    The pair below is cha, not chi. Dropping small kana before looking up the
    pair -- the obvious ordering -- silently turns every name ending in -chan
    into -chin, and this name is routinely transcribed with -chan attached.
    """
    assert name_skeleton("ちゃ") != name_skeleton("ち")
    assert name_skeleton("ちゃん") != name_skeleton("ちん")


@pytest.mark.parametrize("transcript", TRANSCRIBED_CALLS)
def test_every_recorded_call_is_recognized(transcript: str) -> None:
    assert NameMatcher().match(transcript) is not None, f"{transcript!r} was a real call"


@pytest.mark.parametrize("transcript", ORDINARY_SPEECH)
def test_ordinary_speech_never_fires(transcript: str) -> None:
    """Including words that share the name's sounds -- harumi, paruma, haamonika."""
    assert NameMatcher().match(transcript) is None, f"{transcript!r} is not a call"


def test_the_threshold_is_the_one_that_separates_the_two_sets() -> None:
    """Guards the constant: DEFAULT_THRESHOLD must stay above the point of false accepts.

    At 0.75 the recorded ordinary speech starts matching (see the module
    docstring's measurement).
    """
    matcher = NameMatcher(threshold=0.75)
    assert any(matcher.match(t) is not None for t in ORDINARY_SPEECH)
    assert DEFAULT_THRESHOLD > 0.75


def test_threshold_outside_unit_range_raises() -> None:
    with pytest.raises(ValueError, match=r"within \[0, 1\]"):
        NameMatcher(threshold=1.5)


@pytest.mark.parametrize(
    "transcript",
    [
        "ミモザの花",
        "これも読みも同じ",
        "みもとをかくにん",
        "はるみ",
    ],
)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "known exposure: the short form's 4-char skeleton occurs inside "
        "ordinary words, and a lone name-like word shorter than the full "
        "name is compared whole; accepted until re-measured -- see module "
        "docstring"
    ),
)
def test_known_short_form_exposures_are_not_yet_fixed(transcript: str) -> None:
    assert NameMatcher().match(transcript) is None


@pytest.mark.parametrize(
    ("transcript", "command"),
    [
        ("ねえパルミーモ、踊って", "踊って"),
        ("Parumimo dance please", "dance please"),
        ("パルミーモ", ""),
    ],
)
def test_match_returns_the_command_after_the_name(transcript: str, command: str) -> None:
    found = NameMatcher().match(transcript)
    assert found is not None
    assert found.command == command


def test_match_reports_which_name_and_how_well() -> None:
    found = NameMatcher().match("パルミーモ")
    assert found is not None
    assert found.matched == "パルミーモ"
    assert found.score == 1.0


def test_short_form_alone_is_recognized_with_its_command() -> None:
    """A regression that builds match targets from only PALMIMO_NAMES[0] must not pass silently."""
    found = NameMatcher().match("ミーモ、踊って")
    assert found is not None
    assert found.matched == "ミーモ"
    assert found.command == "踊って"


def test_a_long_transcript_is_bounded_rather_than_scanned_forever() -> None:
    """Cost grows with transcript length; a wake word is not buried in an essay."""
    assert NameMatcher().match("あ" * 5000 + "パルミーモ") is None


def test_empty_and_unusable_input_is_not_a_match() -> None:
    matcher = NameMatcher()
    assert matcher.match("") is None
    assert matcher.match("、。！") is None
    assert matcher.match("바로미 뭐") is None  # a call, but rendered in a script we do not fold
