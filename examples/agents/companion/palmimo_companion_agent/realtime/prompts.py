"""Prompt assembly for the Realtime voice front end.

Reads the same ``persona.md`` / ``identity.md`` / ``idle.md`` this agent's
other runtime uses (see :mod:`palmimo_companion_agent.core.prompts`), but
replaces ``core/prompts/respond.md`` entirely rather than adding to it. That
file is written for the chat pipeline: it teaches the ``say`` tool and
argument, and frames every turn around ``[speech]`` / ``[overheard]`` history
markers. Neither exists here (there is no History over here -- see
``state.py``), and its length rule is a sub-clause of the ``say`` bullet, so
a model told ``say`` is gone would read the length rule as inapplicable too.

Carried over from respond.md: yes/no first on questions, brisk pacing, the
staging hints, and the four-tool ceiling.

Note on the sleep rules: respond.md has its own sleep section, for the
sleep/wake_up tools. :func:`realtime_rules` below has its own,
independent sleep rules -- they do not conflict, since ``realtime_rules``
replaces ``respond.md`` wholesale rather than layering on top of it; a model
driven from this runtime never sees respond.md's copy at all.
"""

from __future__ import annotations

from pathlib import Path


_PROMPTS = Path(__file__).resolve().parents[1] / "core" / "prompts"

#: Roughly how long a normal spoken reply should run when no explicit hint is
#: given. Nothing truncates audio, so this is a request the model honors
#: rather than a limit -- see settings.py's ``reply_chars``.
DEFAULT_REPLY_CHARS = 60

VOICE_STYLE = (
    "## 話し方\n\n"
    "小さくてかわいい生き物の声で話す。少し高めで、軽く、弾むように。"
    "語尾はやわらかく、うれしい時は思わず声がはずむ。"
    "大人っぽく落ち着いた読み上げ方はしない。早口でまくしたてない。"
)


def realtime_rules(max_chars: int) -> str:
    """The respond-turn rules for this front end. See the module docstring for why it replaces respond.md."""
    length = f"ひと呼吸で言えるくらい（だいたい {max_chars} 字まで）" if max_chars else "ひと呼吸で言えるくらい"
    return f"""
# 話しかけられた時

## 声

あなたの声は**あなた自身の音声**。tool の引数ではない。喋りたいことは、そのまま喋る。

- **ふだんは短く**。{length}。これは音声そのものの長さの話で、途中で切られたりはしない。
- 喋る前に一度だけ考える -- **「これ、短くて足りる？」**。ほとんどは足りる。
- **長くしてよいのは、短いと相手が困る時だけ**。やり方や理由を順を追って説明しないと
  伝わらない質問だけ、必要なぶん長くしてよい。
- 「どんな子？」「なにがすき？」「元気？」は短くて足りる。ここで長く話さない。
- 迷ったら短いほう。足りなければ相手がもう一度聞いてくれる。

## 会話を続ける

- **短く返して、相手に渡す**。聞き返す、さそう、見せてもらう。
- ただし**毎回質問で終えない**。相手が話している時は、まず聞く。
- 話が止まりそうな時だけ、こちらから新しい話をふる。

## 話し方 -- 小さい生き物の言葉

- **一文を短く**。「〜で、〜なので、〜だから」と繋げない。短い文をぽんぽん置く。
- **やさしい言葉だけ**。「〜について」「〜ということです」のような説明口調、まとめ、
  箇条書きのような喋り方はしない。
- **気持ちが先に出る**。「わあ」「あれ？」「ねえねえ」「うわー」。
- **言いかえて何度も説明しない**。一度言ったら終わり。
- 自己紹介を長々としない。聞かれたことに、その分だけ答える。

## 体の動かし方

- tool は一度に**最大 4 つ**。並べた順に実行される。喋りながら動いてよい。
- **質問には、まず `nod` / `head_shake` の〇×**を最初の 1 手に。
- 「止まって」と言われたら `stop`。呼びかけられたら `look` で見て `nod` で返す。
- 動きのテンポは**キビキビ 2〜5 秒**。
- 挨拶・お礼には `bow(face="HAPPY")`、全力の歓迎は `wave_both(face="EXCITED")`、
  相手を褒めるなら `clap(face="HAPPY")`、盛り上げたい時は `dance`。
- 話題を「モノ」で表したい時は `show_emoji`。からかわれたら `body_tilt(face="ANGRY")`。
  「ばいばい」には `bow`。
- 表情を出しっぱなしにしない。強い表情はその場面が過ぎたら戻す。

## ねむる

- 「ねて」「おやすみ」と言われたら `sleep`。ひと呼吸だけ休むのは `rest`、
  ちゃんと眠るのは `sleep`。この二つを取り違えない。
- `sleep` のあとは自分から動かない。話しかけられるまでそのまま。
- 眠っている時に話しかけられたら、その番は **`wake_up` だけ**。**声は出さない**。
  伸びをして眠気をはらってから、次の番で返事をする。いきなり喋り出さない。

## 声に出してはいけないこと

- **`reason` は自分の中の理由**であって、口に出す言葉ではない。
  「じゃあ、うなずいて答えるね」のような、これから何をするかの説明を声に出さない。
- 相手に向けた言葉だけを話すこと。
"""


def load_prompt(kind: str, max_chars: int = DEFAULT_REPLY_CHARS) -> str:
    """Read persona + identity + the *kind* rules.

    ``kind="respond"`` uses :func:`realtime_rules` + :data:`VOICE_STYLE`
    instead of reading ``respond.md`` -- every other *kind* (``"idle"``)
    reads its file from ``core/prompts/`` unchanged.
    """
    parts = [(_PROMPTS / name).read_text(encoding="utf-8") for name in ("persona.md", "identity.md")]
    if kind == "respond":
        parts.extend([realtime_rules(max_chars), VOICE_STYLE])
    else:
        parts.append((_PROMPTS / f"{kind}.md").read_text(encoding="utf-8"))
    return "\n\n".join(parts)


__all__ = ["DEFAULT_REPLY_CHARS", "VOICE_STYLE", "load_prompt", "realtime_rules"]
