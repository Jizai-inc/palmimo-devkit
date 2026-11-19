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
import contextlib
import sys
from collections.abc import Callable, Coroutine, Iterator
from typing import Protocol

import typer

from palmimo_sdk import PALMIMO_NAMES
from palmimo_sdk.shutdown import (
    TERMINATING_SIGNALS,
    StopRequest,
    interrupt_on_signals,
    park,
    signals_ignored,
    stop_flag_on_signals,
)

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


#: How often an in-flight ``agent.run`` re-checks the stop flag (see
#: :func:`_run_until_stopped`). Short enough that a Ctrl-C during a motion feels
#: immediate, long enough not to spin the event loop for nothing.
_STOP_POLL_S = 0.05


def _run_forever() -> bool:
    """Default :func:`listen_loop` stop predicate: never asks the loop to stop."""
    return False


class _StoppedError(Exception):
    """Internal: *stop* fired while an utterance was still being handled.

    Caught by :func:`listen_loop` ahead of its ``except Exception``, so it ends
    the loop instead of being reported as a transient STT/LLM error.
    """


async def _run_until_stopped[T](coro: Coroutine[object, object, T], stop: Callable[[], bool]) -> T:
    """Await *coro*, cancelling it as soon as *stop* turns true.

    The stop flag is set from a signal handler (see
    :func:`~palmimo_sdk.shutdown.stop_flag_on_signals`), which cannot safely
    touch the event loop, so it is polled from inside the loop here instead.

    Cancelling rather than waiting matters on hardware:
    :meth:`~palmimo_sdk.agent.AgentToolSet.call` catches
    :class:`asyncio.CancelledError` to cancel the motion and then waits for the
    motion worker to settle. Without that, a stop during a walk would let the
    motion run to completion before the loop noticed.

    Raises:
        _StoppedError: If *stop* fired first. The cancellation has already been
            awaited to completion by then, so the robot is settled.
    """
    task = asyncio.ensure_future(coro)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=_STOP_POLL_S)
            if done:
                return task.result()
            if stop():
                await _cancel_and_settle(task)
                raise _StoppedError
    except asyncio.CancelledError:
        # `AgentToolSet.call` only stops the robot when its own coroutine is
        # cancelled, so a cancellation of ours is passed down, not orphaned.
        await _cancel_and_settle(task)
        raise


async def _cancel_and_settle[T](task: asyncio.Task[T]) -> None:
    """Cancel *task* and wait for it to actually finish.

    Its outcome is suppressed -- we are already shutting down, and neither the
    cancellation nor an error the command failed with may replace the reason we
    are leaving. ``KeyboardInterrupt`` is not suppressed: a second signal during
    this wait is the operator saying the graceful path is too slow, and
    :func:`~palmimo_sdk.shutdown.stop_flag_on_signals` has handed the
    disposition back for exactly that.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def _say_best_effort(line: str) -> None:
    """Print *line*, skipping it when stdout is gone (a dropped ssh session raises ``OSError``)."""
    with contextlib.suppress(OSError, ValueError):
        print(line, flush=True)


async def listen_loop[ChunkT](
    chunks: AudioSubscriptionLike[ChunkT],
    *,
    segmenter: _SegmenterLike[ChunkT],
    detector: _DetectorLike,
    stt: Callable[[ChunkT], str],
    agent: _AgentLike,
    say: Callable[[str], None],
    is_speaking: Callable[[], bool],
    stop: Callable[[], bool] = _run_forever,
) -> None:
    """Consume mic *chunks* until *stop*, executing one-shot "<wake word>, <command>" utterances.

    ``async`` because ``CommandAgent.run`` (and the ``AgentToolSet.call`` it
    drives) is a coroutine; every other dependency here
    (``segmenter``, ``detector``, ``stt``, ``say``, ``is_speaking``, and
    iterating *chunks* itself) stays a plain synchronous call -- only
    ``agent.run`` is awaited. Any :class:`Exception` raised by *stt* or
    ``agent.run`` (a transient Whisper/OpenAI hiccup, say) is caught and
    printed so a single bad request can't kill the always-on session.

    *stop* is polled (see :func:`~palmimo_sdk.shutdown.stop_flag_on_signals`);
    returning ``True`` ends the loop normally, so the caller's park runs. It is
    checked once per chunk, after ``stt`` returns (so a stop that arrived during
    that round trip does not start a motion), and before speaking a reply nobody
    is waiting for. ``agent.run`` is already in flight by then, so it is
    cancelled rather than polled, via :func:`_run_until_stopped`.

    While waiting for the *next* chunk the loop blocks in
    ``Subscription.__next__``, so a stop is only seen while audio is flowing. A
    mic that has stopped delivering entirely leaves the wait blocked; the second
    signal, which :func:`~palmimo_sdk.shutdown.stop_flag_on_signals` hands back
    to the disposition underneath, is the way out there.
    """
    for chunk in chunks:
        if stop():
            break
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
            if stop():
                break  # a stop landed during stt -- do not start a motion now
            reply = await _run_until_stopped(agent.run(match.command), stop)
            print(f"[reply] {reply}")
            if stop():
                break
            say(reply)
        except _StoppedError:
            break  # the in-flight command was cancelled and the robot has settled
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
    stop_request = StopRequest()
    stopped_cleanly = False
    # Three layers, outermost first. The ignore keeps SIGTERM/SIGHUP off
    # `SIG_DFL` -- which terminates without unwinding -- from the moment the
    # inner layers hand their dispositions back until the park has finished.
    # `interrupt_on_signals` covers `connect()` and the mic open: torque is on
    # for both, and neither reaches the loop's stop check, so a termination
    # signal there unwinds instead. `stop_flag_on_signals` covers the listen
    # loop, which blocks synchronously and polls the flag.
    with signals_ignored(*TERMINATING_SIGNALS):
        try:
            with interrupt_on_signals(stop_request):
                rt.robot.connect()
                with rt.mic.stream() as chunks, stop_flag_on_signals(stop_request) as stop:
                    asyncio.run(
                        listen_loop(
                            chunks,
                            segmenter=rt.segmenter,
                            detector=rt.detector,
                            stt=rt.stt,
                            agent=rt.agent,
                            say=rt.say,
                            is_speaking=rt.is_speaking,
                            stop=stop,
                        )
                    )
                    if stop():
                        _say_best_effort("\n[stop] signal received; parking the robot...")
            stopped_cleanly = True
        except KeyboardInterrupt:  # a signal outside the listen loop, or a second one inside it
            _say_best_effort("\nExiting.")
        finally:
            # `legs` mirrors `Palmimo.__exit__`: a loop that ended on its stop
            # check returns the legs to neutral first; an interrupt releases
            # torque without streaming more motion at a robot that may already
            # be in a bad state.
            park(rt.robot, legs=stopped_cleanly)


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
