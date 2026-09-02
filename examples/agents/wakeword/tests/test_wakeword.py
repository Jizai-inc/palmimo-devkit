"""Tests for :mod:`palmimo_wakeword_agent.wakeword`."""

from __future__ import annotations

from palmimo_wakeword_agent.wakeword import WakeWordDetector


def test_match_exact_keyword_only_returns_empty_command() -> None:
    detector = WakeWordDetector()
    match = detector.match("パルミーモ")
    assert match is not None
    assert match.command == ""


def test_match_keyword_plus_suffix_extracts_command() -> None:
    detector = WakeWordDetector()
    match = detector.match("パルミーモ前に進んで")
    assert match is not None
    assert match.command == "前に進んで"


def test_match_transcriber_output_with_stray_spaces() -> None:
    # Matching itself is space-tolerant, but the returned command is sliced
    # from the ORIGINAL (NFKC) text, so the inner space from this
    # spaced-out tokenization survives in the asserted command below. Some
    # transcribers may still emit inter-token spaces like this even though
    # the wake-word detector is space-tolerant either way.
    detector = WakeWordDetector()
    match = detector.match("ぱ るみ ーも だんす して")
    assert match is not None
    assert match.command == "だんす して"


def test_match_normalizes_punctuation_and_nfkc() -> None:
    detector = WakeWordDetector()
    match = detector.match("パルミーモ、踊って！")
    assert match is not None
    assert match.command == "踊って"


def test_match_unrelated_text_returns_none() -> None:
    detector = WakeWordDetector()
    assert detector.match("こんにちは、元気ですか") is None


def test_match_finds_first_occurrence_when_keyword_appears_mid_string() -> None:
    detector = WakeWordDetector()
    match = detector.match("ねえパルミーモこっち見て")
    assert match is not None
    assert match.command == "こっち見て"


def test_match_is_case_insensitive_for_latin_rendering() -> None:
    detector = WakeWordDetector()

    lower = detector.match("palmimo dance")
    assert lower is not None
    assert lower.command == "dance"  # whitespace is stripped by normalization

    upper = detector.match("PALMIMO dance")
    assert upper is not None
    assert upper.command == "dance"


def test_match_latin_remainder_keeps_inner_spaces() -> None:
    detector = WakeWordDetector()
    match = detector.match("Palmimo move forward")
    assert match is not None
    assert match.command == "move forward"


def test_match_trims_edge_punctuation_but_not_inner_spaces() -> None:
    detector = WakeWordDetector()
    match = detector.match("Palmimo move forward!")
    assert match is not None
    assert match.command == "move forward"


def test_match_casefold_matching_still_works_with_remainder_mapping() -> None:
    detector = WakeWordDetector()
    match = detector.match("PALMIMO Move Forward")
    assert match is not None
    assert match.command == "Move Forward"


def test_match_japanese_no_space_input_unchanged() -> None:
    detector = WakeWordDetector()
    match = detector.match("パルミーモ前に進んで")
    assert match is not None
    assert match.command == "前に進んで"


def test_match_offset_stays_aligned_when_casefold_expands_characters() -> None:
    # A sharp s casefolds to "ss", making the stripped match string one char
    # longer than its source. The index map must absorb that expansion, or
    # the command slice drifts by one character per expansion.
    detector = WakeWordDetector()
    match = detector.match("Straße Palmimo go home")
    assert match is not None
    assert match.command == "go home"
