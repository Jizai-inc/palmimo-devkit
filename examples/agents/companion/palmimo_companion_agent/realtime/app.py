"""RealtimeSession -- assembles every service and runs the voice session end to end.

The chat front ends (``pipeline.ui.cli`` / ``pipeline.ui.tui``) run a relay --
voice detection, transcription, a chat model, then speech synthesis -- and
each stage waits for the one before it. This front end hands the microphone
to the OpenAI Realtime API instead: the model hears the audio, decides when
the talker has stopped, answers in its own voice, and calls the same tools
the chat agent uses.

Servos, camera, face display and echo cancellation are the real ones. There
is no Speaker: the model's audio is the voice.

    uv run palmimo-realtime --seconds 120

Reads the project's ``.env`` for ``OPENAI_API_KEY``, and takes the servo port
and canceller channels from the same settings the chat agent uses.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
import time
from collections.abc import Iterator
from typing import Any

from palmimo_sdk import (
    DynamixelDriver,
    EchoCanceller,
    FaceDisplay,
    HeadCamera,
    MicStream,
    Palmimo,
    resolve_alsa_device,
)
from palmimo_sdk.agent.toolset import AgentToolSet

from ..core.tools import COMPANION_TOOL_MODELS, make_look_at_face_tool
from ..core.toolview import IDLE_TOOL_NAMES, ToolView
from ..core.vision import FaceLocator, FacePresenceDetector, VisionWatch, WaveDetector
from ..output import configure_output
from .bridge import ToolBridge
from .client import RealtimeClient, RealtimeClientLike
from .log import EventLog
from .prompts import load_prompt
from .protocol import SessionUpdate
from .services.audio import API_RATE, CAPTURE_RATE, MicrophoneFeed, PitchShifter, Playback
from .services.base import Service
from .services.frames import FramePusher, LiveFrame
from .services.idle import IdlePacer, IdleTicker
from .services.reflexes import ReflexRunner, build_reflex_runner
from .services.router import BargeIn, EventRouter, _SessionClosed
from .settings import RealtimeSettings, load_settings
from .state import Sleeping


#: `creep`/`say`/`wave` excluded before this agent's own overrides/composites
#: are registered on top -- `say` because there is no Speaker here (the
#: model's own audio is the voice), `creep` for no companion use case, and
#: `wave` because bare one-arm wave has no companion tool of
#: its own; wave_both covers both the greeting gesture and the wave-back
#: reflex -- see palmimo_companion_agent.pipeline.wiring's own exclude list,
#: which this mirrors).
EXCLUDED_TOOLS = ("creep", "say", "wave")

#: How long shutdown waits for in-flight tool work to settle before it stops
#: waiting. Palmimo.sleep()/wake() do not poll the cancel counter, so a tool
#: inside one cannot be shortened; holding the process past a service
#: manager's stop timeout would get it killed with the robot unparked.
_SETTLE_TIMEOUT_S = 6.0

#: Per 1M tokens, for the cost line printed on exit.
PRICES: dict[str, dict[str, float]] = {
    "gpt-realtime-2.1": {"text_in": 4.00, "audio_in": 32.0, "audio_out": 64.0, "cached_in": 3.2},
    "gpt-realtime-2.1-mini": {"text_in": 0.60, "audio_in": 10.0, "audio_out": 20.0, "cached_in": 1.0},
}


class Usage:
    """Accumulates the token counts the API reports, and prices them."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.text_in = self.audio_in = self.cached_in = self.audio_out = self.responses = 0

    def add(self, usage: dict[str, Any]) -> None:
        self.responses += 1
        details = usage.get("input_token_details", {})
        self.cached_in += details.get("cached_tokens", 0)
        self.text_in += details.get("text_tokens", 0)
        self.audio_in += details.get("audio_tokens", 0)
        self.audio_out += usage.get("output_token_details", {}).get("audio_tokens", 0)

    def dollars(self) -> float:
        price = PRICES.get(self.model)
        if price is None:
            return 0.0
        return (
            self.text_in * price["text_in"]
            + max(0, self.audio_in - self.cached_in) * price["audio_in"]
            + self.cached_in * price["cached_in"]
            + self.audio_out * price["audio_out"]
        ) / 1_000_000

    def report(self, elapsed: float) -> str:
        per_hour = self.dollars() / elapsed * 3600 if elapsed else 0.0
        return f"{self.responses} responses, ${self.dollars():.4f} over {elapsed:.0f}s (${per_hour:.2f}/hour)"


def flatten(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reshape chat-style (``{"type": "function", "function": {...}}``) tool entries into the Realtime shape."""
    return [
        {
            "type": "function",
            "name": entry["function"]["name"],
            "description": entry["function"]["description"],
            "parameters": entry["function"]["parameters"],
        }
        for entry in tools
    ]


def build_robot(settings: RealtimeSettings) -> tuple[Palmimo, MicStream]:
    """Build the robot. No Speaker: the model's audio is the voice.

    ``echo_cancel`` is honoured the same way ``pipeline.wiring`` does it,
    including the empty list: MicStream reads ``processors=None`` as "use my
    default", and its default is an EchoCanceller -- so omitting the
    argument to turn cancellation off would turn it on instead.
    """
    processors = (
        [EchoCanceller(near_channel=settings.near_channel, reference_channel=settings.reference_channel)]
        if settings.echo_cancel
        else []
    )
    mic = MicStream(processors=processors)
    palmimo = Palmimo(driver=DynamixelDriver(port=settings.port), display=FaceDisplay(), camera=HeadCamera(), mic=mic)
    return palmimo, mic


def build_toolset(palmimo: Palmimo, face_locator: FaceLocator) -> AgentToolSet:
    """Register the companion's own tools on top of the SDK's, mirroring ``pipeline.wiring``."""
    toolset = AgentToolSet(palmimo, exclude=EXCLUDED_TOOLS)
    tool_models = dict(COMPANION_TOOL_MODELS)
    tool_models["look_at_face"] = make_look_at_face_tool(face_locator)
    for cls in tool_models.values():
        toolset.register(cls)
    return toolset


def build_vision(palmimo: Palmimo, face_locator: FaceLocator) -> VisionWatch:
    """Watch the camera for waves and faces, sharing *face_locator* with look_at_face.

    FaceLocator serializes its own MediaPipe calls, so the tool's tracking
    loop and the presence detector can hold the same instance.
    """
    camera = palmimo.camera
    assert camera is not None  # build_robot always hands Palmimo a HeadCamera
    detectors: list[WaveDetector | FacePresenceDetector] = [WaveDetector(), FacePresenceDetector(face_locator)]
    return VisionWatch(camera, detectors)


_SIGNALS = tuple(s for s in (getattr(signal, n, None) for n in ("SIGINT", "SIGTERM")) if s is not None)


@contextlib.contextmanager
def _signal_stop(stop: asyncio.Event) -> Iterator[None]:
    """Make SIGINT/SIGTERM end the session promptly, and give the handlers back.

    Setting a flag is not enough: services block on the socket, the camera
    queue, or a mic read rather than polling one, so a flag alone would sit
    unread until ``--seconds`` elapses -- past a service manager's stop
    timeout, which then SIGKILLs the process with the cleanup unrun: no
    return_to_neutral, no neck release, torque left on. So the handler both
    sets *stop* (for anything that does poll it) and cancels the running
    ``asyncio.timeout``/``TaskGroup`` via the event.

    Handlers are removed on the way out. Installing one for SIGINT replaces
    the KeyboardInterrupt disposition, so leaving it in place through a
    shutdown that takes seconds would swallow the operator's second and
    third Ctrl+C -- exactly when a misbehaving robot needs to be killed.
    """
    loop = asyncio.get_running_loop()
    installed = []
    for sig in _SIGNALS:
        with contextlib.suppress(NotImplementedError):  # not available on Windows
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
    try:
        yield
    finally:
        for sig in installed:
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(sig)


class _Stop(Exception):  # noqa: N818 -- internal control-flow signal, not a reported error
    """Raised by :meth:`RealtimeSession._watch_stop` to end the session's task group promptly."""


async def _wake_and_disconnect(palmimo: Palmimo, sleeping: Sleeping) -> None:
    """Park the robot: wake first if asleep, then disconnect.

    Shared between :meth:`RealtimeSession._shutdown` (the common case: the
    session ran, and this is the last step of its own unwind) and ``_run``'s
    outer ``finally`` (the field-common failure case: something between a
    successful ``Palmimo.connect()`` and the session's first
    ``await session.run(...)`` -- most often ``RealtimeClient.connect()``
    itself, on a bad key or no network -- raised before a
    :class:`RealtimeSession` even existed to park on its own). ``_run`` only
    calls this when no session was built, so the two callers never race or
    double-park the same robot.

    A robot that is *asleep* is woken before the park, because
    ``disconnect()`` parks by streaming the stand-up pose, and asleep means
    the legs are on the reduced-gain ``sleep()`` left them at -- seconds of a
    goal they cannot reach. If waking fails, the park is skipped rather than
    fought.
    """
    if sleeping.asleep:
        try:
            await asyncio.to_thread(palmimo.wake)
        except Exception:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(palmimo.disconnect, park=False)
            return
    with contextlib.suppress(Exception):
        await asyncio.to_thread(palmimo.disconnect)


class RealtimeSession:
    """Owns every live dependency for one Realtime voice session, and its shutdown."""

    def __init__(
        self,
        *,
        client: RealtimeClientLike,
        services: list[Service],
        bridge: ToolBridge,
        playback: Playback,
        watch: VisionWatch,
        palmimo: Palmimo,
        sleeping: Sleeping,
        usage: Usage,
        frame: LiveFrame,
        reflexes: ReflexRunner | None = None,
    ) -> None:
        self._client = client
        self._services = services
        self._bridge = bridge
        self._playback = playback
        self._watch = watch
        self._palmimo = palmimo
        self._sleeping = sleeping
        self._usage = usage
        self._frame = frame
        self._reflexes = reflexes
        self._stop = asyncio.Event()

    async def _watch_stop(self) -> None:
        await self._stop.wait()
        raise _Stop

    async def run(self, seconds: float) -> None:
        """Run every service until *seconds* elapse, a service raises, or a signal arrives.

        ``_shutdown`` runs OUTSIDE ``_signal_stop``'s ``with`` block, after
        the signal handlers are removed -- not inside it. ``_signal_stop``'s
        whole point is giving the operator's SIGINT/SIGTERM disposition back
        before a shutdown that can itself take seconds (settling tool work,
        closing the audio pipe, parking the robot); nesting the ``finally``
        inside the ``with`` would keep the handler installed for that whole
        stretch and swallow a 2nd/3rd Ctrl+C exactly when a misbehaving robot
        needs to be killed.

        ``_SessionClosed`` (raised by :class:`~.services.router.EventRouter`
        when the server closes the socket) joins ``_Stop``/``TimeoutError``
        here as a normal way the session ends, not an error to surface --
        see that exception's own docstring for why a bare return from the
        router would otherwise leave the other services running.
        """
        started = time.monotonic()
        try:
            with _signal_stop(self._stop):
                try:
                    async with asyncio.timeout(seconds), asyncio.TaskGroup() as tg:
                        tg.create_task(self._watch_stop())
                        for service in self._services:
                            tg.create_task(service.run())
                except* (_Stop, TimeoutError, _SessionClosed):
                    pass
        finally:
            await self._shutdown()
        print(f"\n{self._usage.report(time.monotonic() - started)}", flush=True)
        print(f"frames sent: {self._frame.pushed}", flush=True)

    async def _shutdown(self) -> None:
        """End the session with the robot in a safe, known state. Order is the whole point.

        :meth:`~.bridge.ToolBridge.settle` covers every tool task the model
        started (the ``TaskGroup`` has already cancelled the services
        themselves, including the reflex runner, which dispatches
        ``wave_both`` through the same toolset). :meth:`~.services.reflexes.ReflexRunner.settle`
        covers the reflex engine's own detached notify-send tasks
        separately -- those are not children of the reflex service's own
        task, so the ``TaskGroup`` cancelling it never touches them (see
        that method's docstring). Draining audio comes after the motions,
        not before: :meth:`~.services.audio.Playback.close` blocks the loop
        for seconds and nothing can be cancelled meanwhile. The
        wake-before-park step itself is :func:`_wake_and_disconnect`, shared
        with ``_run``'s own outer ``finally`` -- see its docstring.
        """
        await self._bridge.settle(_SETTLE_TIMEOUT_S)
        if self._reflexes is not None:
            await self._reflexes.settle(_SETTLE_TIMEOUT_S)
        self._playback.close()
        with contextlib.suppress(Exception):
            await self._watch.aclose()
        await _wake_and_disconnect(self._palmimo, self._sleeping)


async def _run(args: argparse.Namespace) -> int:
    overrides = {"port": args.port, "log_path": args.log_path}
    settings = load_settings(**{k: v for k, v in overrides.items() if v is not None})
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY is not set; put it in the companion's .env", file=sys.stderr)
        return 2

    log = EventLog.open(settings.log_path)
    instructions = load_prompt("respond", args.reply_chars)
    idle_prompt = load_prompt("idle")
    palmimo, _mic = build_robot(settings)
    face_locator = FaceLocator()
    toolset = build_toolset(palmimo, face_locator)
    respond_view = ToolView(toolset, squash_say=True)
    idle_tools = flatten(ToolView(toolset, allow=IDLE_TOOL_NAMES, squash_say=True).to_openai_tools())
    tools = flatten(respond_view.to_openai_tools())
    sleeping = Sleeping()
    usage = Usage(args.model)
    frame = LiveFrame(palmimo.camera)
    watch = build_vision(palmimo, face_locator)

    await asyncio.to_thread(palmimo.connect)
    # `session` stays None until RealtimeSession is actually constructed.
    # Anything between here and that point raising -- most commonly
    # RealtimeClient.connect() itself, on a bad key or no network -- must
    # still park the robot: the outer `finally` below owns that case, since
    # once `session` exists, `session.run()`'s own `finally` (RealtimeSession
    # ._shutdown) always runs before this coroutine can unwind any further.
    session: RealtimeSession | None = None
    try:
        async with RealtimeClient.connect(model=args.model, api_key=key) as client:
            await client.send(
                SessionUpdate(
                    instructions=instructions,
                    tools=tools,
                    voice=args.voice,
                    rate=API_RATE,
                    transcription_language=settings.language,
                )
            )
            pitch = PitchShifter(args.pitch) if args.pitch != 1.0 else None
            # Resolved here, not inside Playback: the session is already up, so
            # the card listing is settled, and a resolved string keeps the
            # writer thread's hot path free of a subprocess call.
            playback = Playback(pitch, device=resolve_alsa_device(settings.speaker_device, kind="playback"))
            pacer = IdlePacer()
            bridge = ToolBridge(client, toolset, sleeping, log)
            barge_in = BargeIn(playback, bridge, pacer)
            router = EventRouter(client, playback, barge_in, bridge, pacer, sleeping, usage, log)
            idle = IdleTicker(client, pacer, sleeping, idle_prompt, idle_tools, log)
            frames_service = FramePusher(client, frame, pacer, sleeping, args.frame_seconds, log)
            mic_feed = MicrophoneFeed(_mic, client)
            reflexes = build_reflex_runner(toolset, watch, client, sleeping, log)

            print(
                f"{args.model}, voice {args.voice}, pitch x{args.pitch:.2f}, {len(tools)} tools, "
                f"a frame every {args.frame_seconds:.0f}s, {args.seconds:.0f}s\n",
                flush=True,
            )
            session = RealtimeSession(
                client=client,
                services=[mic_feed, router, idle, frames_service, reflexes],
                bridge=bridge,
                playback=playback,
                watch=watch,
                palmimo=palmimo,
                sleeping=sleeping,
                usage=usage,
                frame=frame,
                reflexes=reflexes,
            )
            await session.run(args.seconds)
    finally:
        if session is None:
            await _wake_and_disconnect(palmimo, sleeping)
        log.close()
    return 0


def main() -> int:
    """Console-script entry point (``palmimo-realtime``).

    :func:`~palmimo_companion_agent.output.configure_output` runs first, for the
    same reason it does at the chat entry point: every line this front end
    prints today passes ``flush=True``, but that is a per-call-site guarantee
    the next line added has to remember, and it cannot reach output produced
    inside the SDK at all. Setting line buffering on the stream covers both.
    """
    configure_output()

    from .settings import VOICES

    parser = argparse.ArgumentParser(description="Run the companion on an OpenAI Realtime voice session.")
    parser.add_argument("--model", default=None, help="OpenAI Realtime model (env: COMPANION_AGENT_MODEL)")
    parser.add_argument("--voice", default=None, choices=VOICES, help="Session voice (env: COMPANION_AGENT_VOICE)")
    parser.add_argument(
        "--pitch", type=float, default=None, help="Voice pitch multiplier; 1.0 is off (env: COMPANION_AGENT_PITCH)"
    )
    parser.add_argument(
        "--reply-chars",
        type=int,
        default=None,
        help="0 leaves length to judgement (env: COMPANION_AGENT_REPLY_CHARS)",
    )
    parser.add_argument(
        "--frame-seconds",
        type=float,
        default=None,
        help="Seconds between camera frames sent to the model (env: COMPANION_AGENT_FRAME_SECONDS)",
    )
    parser.add_argument(
        "--seconds", type=float, default=None, help="Session length in seconds (env: COMPANION_AGENT_SESSION_SECONDS)"
    )
    parser.add_argument("--port", default=None, help="Servo bus serial port (env: COMPANION_AGENT_PORT)")
    parser.add_argument("--log-path", default=None, help="JSONL event log file path (env: COMPANION_AGENT_LOG_PATH)")
    parsed = parser.parse_args()

    defaults = load_settings()
    args = argparse.Namespace(
        model=parsed.model if parsed.model is not None else defaults.model,
        voice=parsed.voice if parsed.voice is not None else defaults.voice,
        pitch=parsed.pitch if parsed.pitch is not None else defaults.pitch,
        reply_chars=parsed.reply_chars if parsed.reply_chars is not None else defaults.reply_chars,
        frame_seconds=parsed.frame_seconds if parsed.frame_seconds is not None else defaults.frame_seconds,
        seconds=parsed.seconds if parsed.seconds is not None else defaults.session_seconds,
        port=parsed.port,
        log_path=parsed.log_path,
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "CAPTURE_RATE",
    "EXCLUDED_TOOLS",
    "PRICES",
    "RealtimeSession",
    "Usage",
    "build_robot",
    "build_toolset",
    "build_vision",
    "flatten",
    "main",
]
