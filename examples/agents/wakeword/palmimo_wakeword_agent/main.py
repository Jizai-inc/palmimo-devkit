"""Wake-word voice agent: mic chunks -> VAD segmentation -> STT -> wake-word
match -> tool-calling agent -> spoken reply.

One-shot only: the wake word and the command must arrive in the same breath
("<wake word>, <command>"). An utterance containing only the wake word (no
command text after it) is ignored, and so is any utterance that never
mentions the wake word at all. All the real-component wiring lives in
:mod:`palmimo_wakeword_agent.wiring`; this module holds only the listen loop
and its CLI.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Iterator
from typing import Protocol

import typer

from palmimo_sdk import PALMIMO_NAMES

from .output import configure_output
from .settings import WakewordAgentSettings
from .wakeword import WakeMatch
from .wiring import build_runtime


# The loop is generic over the audio payload (ChunkT): real runs carry int16
# numpy arrays end to end, while tests drive the same loop with plain string
# sentinels — the loop itself never looks inside a chunk.
class _SegmenterLike[ChunkT](Protocol):
    def feed(self, chunk: ChunkT) -> ChunkT | None: ...
    def reset(self) -> None: ...


class _DetectorLike(Protocol):
    def match(self, text: str) -> WakeMatch | None: ...


class _AgentLike(Protocol):
    async def run(self, text: str) -> str: ...


class AudioSubscriptionLike[ChunkT](Protocol):
    """Structural surface of a :class:`~palmimo_sdk.Subscription` we depend on here."""

    dropped: int

    def __iter__(self) -> Iterator[ChunkT]: ...
    def get(self, timeout: float | None = None) -> ChunkT | None: ...


async def listen_loop[ChunkT](
    chunks: AudioSubscriptionLike[ChunkT],
    *,
    segmenter: _SegmenterLike[ChunkT],
    detector: _DetectorLike,
    stt: Callable[[ChunkT], str],
    agent: _AgentLike,
    say: Callable[[str], None],
    is_speaking: Callable[[], bool],
) -> None:
    """Consume mic *chunks* forever, executing one-shot "<wake word>, <command>" utterances.

    ``async`` because ``CommandAgent.run`` (and the ``AgentToolSet.call`` it
    drives) is a coroutine; every other dependency here
    (``segmenter``, ``detector``, ``stt``, ``say``, ``is_speaking``, and
    iterating *chunks* itself) stays a plain synchronous call -- only
    ``agent.run`` is awaited. Any :class:`Exception` raised by *stt* or
    ``agent.run`` (a transient Whisper/OpenAI hiccup, say) is caught and
    printed so a single bad request can't kill the always-on session.
    """
    for chunk in chunks:
        if is_speaking():  # never transcribe our own TTS
            segmenter.reset()
            continue
        utterance = segmenter.feed(chunk)
        if utterance is None:
            continue
        try:
            text = stt(utterance)
            print(f"[heard] {text}")
            match = detector.match(text)
            if match is None or not match.command:
                continue  # no wake word, or the wake word alone
            reply = await agent.run(match.command)
            print(f"[reply] {reply}")
            say(reply)
        except Exception as exc:  # a transient STT/LLM error must not kill the session
            print(f"[error] {exc!r}")
        finally:
            # Even an ignored utterance spent a network round-trip not reading
            # the mic; always restart from live audio, never a stale backlog.
            _discard_stale_audio(chunks, segmenter)


def _discard_stale_audio[ChunkT](chunks: AudioSubscriptionLike[ChunkT], segmenter: _SegmenterLike[ChunkT]) -> None:
    """Drop whatever piled up on *chunks* while an utterance was being handled.

    Nobody reads the mic queue while :func:`listen_loop` is busy transcribing,
    running tool calls, and speaking the reply -- it fills up and starts
    dropping the oldest chunks. Feeding that stale, possibly spliced backlog
    into the segmenter afterwards would produce garbage utterances, so this
    drains it (each ``get(timeout=0)`` returns immediately, ``None`` once
    empty), resets the segmenter, and prints a one-line warning if the
    subscription actually lost audio while we were busy.
    """
    dropped_before = chunks.dropped
    while chunks.get(timeout=0) is not None:
        pass
    segmenter.reset()
    if chunks.dropped > dropped_before:
        print(f"[warn] {chunks.dropped - dropped_before} mic chunks dropped while busy")


def _missing_api_key_error(settings: WakewordAgentSettings) -> str | None:
    """Return a preflight error message if a required API key is missing, else ``None``.

    STT always runs against OpenAI's Whisper API, so ``OPENAI_API_KEY`` is
    unconditionally required. ``GEMINI_API_KEY`` is only required when
    ``settings.command_model`` routes to Google's OpenAI-compatible endpoint
    (see :func:`palmimo_wakeword_agent.wiring._chat_client_for`) -- pointing
    ``command_model`` back at a plain OpenAI model needs only the OpenAI key.
    Pure and hardware-free so it's cheap to unit-test independently of the
    typer CLI plumbing.
    """
    if not settings.openai_api_key:
        return "Error: OPENAI_API_KEY is not set (environment variable or .env)."
    if settings.command_model.startswith("gemini-") and not settings.gemini_api_key:
        return "Error: GEMINI_API_KEY is not set (environment variable or .env)."
    return None


app = typer.Typer(add_completion=False)


@app.command()
def main(
    device: str | None = typer.Option(
        None, help="Input device name substring hint (MicStream device_name_hint; env: WAKEWORD_AGENT_DEVICE)"
    ),
    speaker_device: str | None = typer.Option(
        None,
        help=(
            "Output device name substring hint (SpeakerConfig device_name_hint; "
            "env: WAKEWORD_AGENT_SPEAKER_DEVICE; default: ReSpeaker). Pass an "
            "empty string to use the ALSA default."
        ),
    ),
    command_model: str | None = typer.Option(
        None,
        help=("Chat model for command execution (env: WAKEWORD_AGENT_COMMAND_MODEL; default: gemini-3.5-flash-lite)"),
    ),
    stt_model: str | None = typer.Option(
        None,
        help="Whisper-family transcription model (env: WAKEWORD_AGENT_STT_MODEL; default: gpt-4o-mini-transcribe)",
    ),
    language: str | None = typer.Option(
        None,
        help=(
            "ISO 639-1 language code for STT, TTS voice, and reply language "
            "(env: WAKEWORD_AGENT_LANGUAGE; default: en). The SDK speaker "
            "currently ships voices for 'en' and 'ja'."
        ),
    ),
    tts: bool | None = typer.Option(
        None, "--tts/--no-tts", help="Speak replies aloud (env: WAKEWORD_AGENT_TTS; default: true)"
    ),
    servo: bool | None = typer.Option(
        None, "--servo/--no-servo", help="Attach the servo driver (env: WAKEWORD_AGENT_SERVO; default: true)"
    ),
    servo_port: str | None = typer.Option(
        None, help="Servo bus serial port, e.g. /dev/ttyACM0 (env: WAKEWORD_AGENT_SERVO_PORT; default: auto-detected)"
    ),
) -> None:
    """Palmimo wake-word voice agent: listen, transcribe, and execute one-shot spoken commands."""
    settings = WakewordAgentSettings().merged(
        device=device,
        speaker_device=speaker_device,
        command_model=command_model,
        stt_model=stt_model,
        language=language,
        tts=tts,
        servo=servo,
        servo_port=servo_port,
    )
    error = _missing_api_key_error(settings)
    if error is not None:
        print(error, file=sys.stderr)
        raise typer.Exit(1)

    rt = build_runtime(settings)
    print(f'Palmimo wake-word agent started. Say "{PALMIMO_NAMES[0]} <command>" in one breath.')
    try:
        with rt.robot, rt.mic.stream() as chunks:
            asyncio.run(
                listen_loop(
                    chunks,
                    segmenter=rt.segmenter,
                    detector=rt.detector,
                    stt=rt.stt,
                    agent=rt.agent,
                    say=rt.say,
                    is_speaking=rt.is_speaking,
                )
            )
    except KeyboardInterrupt:
        print("\nExiting.")


def run() -> None:
    """Console-script entry point (``[project.scripts]``): set output up, then invoke the typer app.

    :func:`~palmimo_wakeword_agent.output.configure_output` runs before the CLI
    so that everything the agent prints -- including a startup error raised by
    the command below -- reaches a redirected stdout as it happens. Only the
    application configures this; importing the package changes nothing.
    """
    configure_output()
    app()


if __name__ == "__main__":  # pragma: no cover
    run()
