"""Transcripts not worth an answer: filler words and the phrases STT invents from silence.

The same early-reject rules as the companion agent's guard, without its extra
model call: everything that passes these is answered.
"""

from __future__ import annotations

import re


#: Back-channel sounds a transcriber often produces from breath or room noise.
_BACKCHANNELS: frozenset[str] = frozenset(
    {"はい", "うん", "ええ", "あー", "えー", "おー", "ねえ", "そう", "うわ", "おお", "わー", "ん", "あ", "え", "お"}
)

#: Video sign-offs a transcriber hallucinates from silence, music, or applause.
_HALLUCINATIONS: tuple[str, ...] = (
    "ご視聴ありがとうございました",
    "ご清聴ありがとうございました",
    "最後までご視聴",
    "チャンネル登録",
    "thanks for watching",
    "please subscribe",
)

#: Shorter than this is almost always an artifact; the shortest real request, a four-character "stop", clears it.
_MIN_LENGTH = 3

#: Scripts never used in Japanese or English input, hallucinated from crowd noise.
_FOREIGN_SCRIPT = re.compile("[Ѐ-ӿ֐-׿؀-ۿऀ-ॿ฀-๿ᄀ-ᇿ㄰-㆏가-힣]")

#: Trailing punctuation STT adds to a lone filler word, such as a full stop after "yes".
_TRAILING_PUNCTUATION = "。、！？!?.,… "


def is_noise(text: str) -> bool:
    """Whether *text* is a transcript the robot should not reply to."""
    stripped = text.strip()
    bare = stripped.rstrip(_TRAILING_PUNCTUATION)
    return (
        bare in _BACKCHANNELS
        or any(phrase in stripped.lower() for phrase in _HALLUCINATIONS)
        or _FOREIGN_SCRIPT.search(stripped) is not None
        or len(bare) < _MIN_LENGTH
    )


__all__ = ["is_noise"]
