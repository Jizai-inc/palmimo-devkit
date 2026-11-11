"""AiController: runs one AI mode at a time as an OpenAI Realtime voice session.

The visitor's speech streams to the Realtime model, which hears it directly
and answers in its own voice; a reply's tool calls become motion intents in
`session.PilotSession`.

Runs on the server's event loop, the same loop the WebSocket handler writes
`session.PilotSession` from, so the session keeps a single writer. The robot
connection belongs to `robot_loop.RobotLoop` and is never touched here:
switching modes only changes which input the robot loop reads.

Any failure (no key, rejected key, quota, network, mic, a slow start) ends the
mode, puts the session back in teleop with a plain-language notice, and
leaves the robot connected.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import Callable, Hashable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from typing import Any, NamedTuple, Protocol, TypeVar

from ..modes import PromptSource
from ..session import PilotSession
from ..telemetry import Telemetry
from ..voices import DEFAULT_VOICE
from . import protocol
from .audio import API_RATE, MicLevelLike, MicrophoneFeed, MicStreamLike
from .client import RealtimeClient, RealtimeClientLike
from .frames import SEE_TOOL, VISION_NOTE, FramePusher, LiveFrame
from .protocol import ItemCreate, ResponseCreate, SessionUpdate
from .tools import TOOL_DEFINITIONS, run_tool


_LOG = logging.getLogger(__name__)

#: Realtime model. `gpt-realtime-2.1-mini` costs about a third as much, but
#: follows the persona and picks motions less reliably.
DEFAULT_MODEL = "gpt-realtime-2.1"

#: Longest the connection at a switch to AI may take before it is abandoned.
START_TIMEOUT_S: float = 8.0

#: Without echo cancellation the room still rings briefly after playback ends;
#: listening again any sooner lets the model hear the end of its own sentence.
_ECHO_TAIL_S = 0.4

# Messages the pilot sees when AI stops. Shown on the pilot page as-is.
NOTICE_NO_KEY = "AIは使用できません：ポータルでAPIキーを設定してください"
NOTICE_BAD_KEY = "AIは使用できません：ポータルでAPIキーを確認してください"
NOTICE_QUOTA = "AIは使用できません：OpenAIの利用上限に達しました"
NOTICE_NETWORK = "AIとの接続が切れました。手動操作に戻りました"
NOTICE_TIMEOUT = "AIの起動に時間がかかりすぎました。もう一度お試しください"
NOTICE_NO_PROMPT = "このモードは読み込めませんでした"
NOTICE_MIC = "マイクを開けませんでした。手動操作に戻りました"
NOTICE_ROBOT_OFFLINE = "ロボットとの接続に問題があるため、AIを停止しました"
NOTICE_FAILED = "AIでエラーが発生しました。手動操作に戻りました"

#: Realtime `error` codes that mean the key cannot work, so carrying on is pointless.
_QUOTA_CODES = frozenset({"insufficient_quota"})
_KEY_CODES = frozenset({"invalid_api_key"})

_T = TypeVar("_T")


class OpenMic(NamedTuple):
    """An open microphone, and whether its capture cancels the robot's own voice."""

    stream: MicStreamLike
    echo_cancelled: bool


class PlaybackLike(Protocol):
    """The part of `audio.Playback` the controller uses."""

    def write(self, pcm: bytes) -> None: ...

    def interrupt(self) -> None: ...

    def speaking(self, *, tail: float = 0.0) -> bool: ...

    def close(self) -> None: ...


class ReflexesLike(Protocol):
    """The part of `reflexes.Reflexes` (and `idle.Idle`) the controller runs."""

    async def run(self) -> None: ...


Connect = Callable[..., AbstractAsyncContextManager[RealtimeClientLike]]
#: Builds a session's reflexes; called with `on_new_face=` when frames are on,
#: so a newly noticed face sends the model a frame at once.
ReflexesFactory = Callable[..., ReflexesLike]


class _AiStopped(Exception):  # noqa: N818 -- control flow carrying the pilot's notice
    def __init__(self, notice: str, reason: str) -> None:
        super().__init__(reason)
        self.notice = notice
        self.reason = reason


def _classify(exc: BaseException) -> tuple[str, str]:
    """Map a failure to `(notice for the pilot, short reason for telemetry)`."""
    if isinstance(exc, _AiStopped):
        return exc.notice, exc.reason
    if isinstance(exc, TimeoutError):
        return NOTICE_TIMEOUT, "start_timeout"
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return NOTICE_BAD_KEY, f"http_{status}"
    if status == 429:
        return NOTICE_QUOTA, "http_429"
    if isinstance(exc, OSError) or type(exc).__name__.startswith("ConnectionClosed"):
        return NOTICE_NETWORK, "network"
    return NOTICE_FAILED, type(exc).__name__


class AiController:
    """Owns at most one running AI mode at a time."""

    def __init__(
        self,
        session: PilotSession,
        prompts: PromptSource,
        *,
        api_key: str | None,
        telemetry: Telemetry,
        mic_factory: Callable[[], AbstractContextManager[OpenMic]],
        playback_factory: Callable[[], PlaybackLike],
        connect: Connect = RealtimeClient.connect,
        model: str = DEFAULT_MODEL,
        voice: Callable[[], str] = lambda: DEFAULT_VOICE,
        start_timeout: float = START_TIMEOUT_S,
        now: Callable[[], float] = time.monotonic,
        reflexes_factory: ReflexesFactory | None = None,
        idle_factory: Callable[[], ReflexesLike] | None = None,
        mic_gain: Callable[[], float] = lambda: 1.0,
        mic_level: MicLevelLike | None = None,
        frame_source: Callable[[], bytes | None] | None = None,
    ) -> None:
        self._session = session
        self._prompts = prompts
        self._api_key = api_key or None
        self._telemetry = telemetry
        self._mic_factory = mic_factory
        self._playback_factory = playback_factory
        self._connect = connect
        self._model = model
        self._voice = voice
        self._start_timeout = start_timeout
        self._now = now
        self._reflexes_factory = reflexes_factory
        self._idle_factory = idle_factory
        self._mic_gain = mic_gain
        self._mic_level = mic_level
        #: The newest head-camera JPEG, or `None` to keep the model blind (see `frames`).
        self._frame_source = frame_source
        #: This session's frame, while a session with frames on runs.
        self._live_frame: LiveFrame | None = None
        #: Whether a response is being generated, so a frame waits for the robot to be quiet.
        self._responding = False
        self._task: asyncio.Task[None] | None = None
        # Per-response bookkeeping for the `ai_response` debug event only;
        # nothing here changes how a turn is handled.
        self._response_started: dict[str, float] = {}
        self._response_audio_bytes = 0
        self._current_response_id = ""

    @property
    def available(self) -> bool:
        """Whether an API key is configured. Teleop never depends on this."""
        return self._api_key is not None

    @property
    def running(self) -> bool:
        """Whether an AI mode task is in flight (starting or live)."""
        return self._task is not None and not self._task.done()

    async def start(self, client_id: Hashable, mode_id: str) -> bool:
        """Switch to AI mode *mode_id*.

        Returns:
            bool: `False` if the switch was ignored (not the pilot, or a
                switch already in progress) or failed before it began.
        """
        previous = self._session.mode
        if not self._session.begin_ai(client_id, mode_id):
            return False
        if self._api_key is None:
            self._fail(previous, mode_id, NOTICE_NO_KEY, "no_api_key", latency_ms=0)
            return False
        try:
            prompt = self._prompts.prompt(mode_id)
        except Exception:
            _LOG.exception("could not load prompt for mode %r", mode_id)
            self._fail(previous, mode_id, NOTICE_NO_PROMPT, "prompt_unavailable", latency_ms=0)
            return False
        # Read at each start, like the prompt, so a choice saved on the homepage applies next time.
        voice = self._voice()
        self._task = asyncio.create_task(
            self._run(self._api_key, mode_id, prompt, voice, self._now()), name=f"ai-mode-{mode_id}"
        )
        return True

    async def stop(self, *, notice: str | None = None, reason: str = "user") -> None:
        """End the AI mode (if any) and return to teleop. Safe to call at any time."""
        task, self._task = self._task, None
        from_mode = self._session.ai_mode
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if from_mode is not None:
            self._session.end_ai(notice)
            self._telemetry.event("mode_switch", from_mode=from_mode, to_mode="teleop", ok=True, reason=reason)

    def _fail(self, from_mode: str, mode_id: str, notice: str, reason: str, *, latency_ms: int) -> None:
        self._session.end_ai(notice)
        self._telemetry.event(
            "mode_switch", from_mode=from_mode, to_mode=mode_id, ok=False, latency_ms=latency_ms, error=reason
        )

    async def _run(self, api_key: str, mode_id: str, prompt: str, voice: str, started_at: float) -> None:
        live = False
        playback: PlaybackLike | None = None
        try:
            async with contextlib.AsyncExitStack() as stack:
                mic = await self._enter(stack, self._mic_factory(), NOTICE_MIC, "mic_unavailable")
                async with asyncio.timeout(self._start_timeout):
                    client = await stack.enter_async_context(self._connect(model=self._model, api_key=api_key))
                    vision = self._frame_source is not None
                    await client.send(
                        SessionUpdate(
                            instructions=prompt + VISION_NOTE if vision else prompt,
                            tools=[*TOOL_DEFINITIONS, SEE_TOOL] if vision else TOOL_DEFINITIONS,
                            voice=voice,
                            rate=API_RATE,
                        )
                    )
                # Resolving the output device lists ALSA cards, which blocks.
                playback = await asyncio.to_thread(self._playback_factory)
                self._session.ai_started()
                live = True
                self._telemetry.event(
                    "mode_switch",
                    from_mode="teleop",
                    to_mode=mode_id,
                    ok=True,
                    latency_ms=round((self._now() - started_at) * 1000),
                )
                speaker = playback
                hold = None if mic.echo_cancelled else (lambda: speaker.speaking(tail=_ECHO_TAIL_S))
                self._responding = False
                pusher: FramePusher | None = None
                if self._frame_source is not None:
                    self._live_frame = LiveFrame(self._frame_source)
                    pusher = FramePusher(
                        self._live_frame,
                        client,
                        busy=lambda: self._responding or speaker.speaking(),
                        telemetry=self._telemetry,
                    )
                async with asyncio.TaskGroup() as group:
                    group.create_task(
                        MicrophoneFeed(mic.stream, client, hold=hold, gain=self._mic_gain, level=self._mic_level).run()
                    )
                    group.create_task(self._route(client, playback))
                    if pusher is not None:
                        # FramePusher.run() only ever ends by cancellation.
                        group.create_task(pusher.run())
                    if self._reflexes_factory is not None:
                        # Reflexes.run() only ever ends by cancellation, so it
                        # cannot take the mode down with it.
                        reflexes = (
                            self._reflexes_factory(on_new_face=pusher.nudge)
                            if pusher is not None
                            else self._reflexes_factory()
                        )
                        group.create_task(reflexes.run())
                    if self._idle_factory is not None:
                        # Idle.run() likewise only ends by cancellation.
                        group.create_task(self._idle_factory().run())
        except Exception as exc:
            # The TaskGroup wraps a task's failure in an ExceptionGroup;
            # the first one is what ended the mode.
            failure: BaseException = exc.exceptions[0] if isinstance(exc, ExceptionGroup) and exc.exceptions else exc
            notice, reason = _classify(failure)
            _LOG.warning("AI mode %r stopped: %s", mode_id, reason, exc_info=failure)
            if live:
                self._session.end_ai(notice)
                self._telemetry.event("ai_error", mode=mode_id, error=reason)
                self._telemetry.event("mode_switch", from_mode=mode_id, to_mode="teleop", ok=True, reason=reason)
            else:
                self._fail("teleop", mode_id, notice, reason, latency_ms=round((self._now() - started_at) * 1000))
            if self._task is asyncio.current_task():
                self._task = None
        finally:
            self._live_frame = None
            if playback is not None:
                playback.interrupt()
                await asyncio.to_thread(playback.close)

    async def _enter(
        self, stack: contextlib.AsyncExitStack, manager: AbstractContextManager[_T], notice: str, reason: str
    ) -> _T:
        """Enter *manager* on a worker thread and register its exit on *stack*.

        Opening blocks (device setup, and a model download on first use), so
        it must not run on the event loop, which also serves every pilot and
        viewer connection. If the switch is cancelled while the open is still
        running, the resource is closed as soon as the open finishes.
        """
        opening = asyncio.ensure_future(asyncio.to_thread(manager.__enter__))
        try:
            value = await asyncio.shield(opening)
        except Exception as exc:
            raise _AiStopped(notice, reason) from exc
        except asyncio.CancelledError:

            def close_when_open(future: asyncio.Future[_T]) -> None:
                if not future.cancelled() and future.exception() is None:
                    manager.__exit__(None, None, None)

            opening.add_done_callback(close_when_open)
            raise
        stack.push(manager.__exit__)
        return value

    async def _route(self, client: RealtimeClientLike, playback: PlaybackLike) -> None:
        async for event in client.events():
            await self._dispatch(client, playback, event)
        raise _AiStopped(NOTICE_NETWORK, "socket_closed")

    async def _dispatch(
        self, client: RealtimeClientLike, playback: PlaybackLike, event: protocol.ServerEvent | protocol.Unknown
    ) -> None:
        match event:
            case protocol.AudioDelta():
                pcm = base64.b64decode(event.delta)
                self._response_audio_bytes += len(pcm)
                playback.write(pcm)
            case protocol.SpeechStarted():
                cut_off = playback.speaking()
                # The visitor started talking: stop speaking over them.
                playback.interrupt()
                self._telemetry.event("visitor_speech", state="started", interrupted_robot=cut_off)
            case protocol.SpeechStopped():
                self._telemetry.event("visitor_speech", state="stopped")
            case protocol.ResponseCreated():
                self._responding = True
                self._response_started[event.response.id] = self._now()
                self._response_audio_bytes = 0
                self._current_response_id = event.response.id
            case protocol.TranscriptDone():
                # The robot's own words only; visitor speech is never transcribed.
                _LOG.info("AI said: %s", event.transcript)
                self._telemetry.event("ai_said", response_id=self._current_response_id, text=event.transcript)
            case protocol.ResponseDone():
                self._responding = False
                self._log_response(event.response)
                calls = event.response.function_calls
                saw_photo = False
                for call in calls:
                    if call.name == SEE_TOOL["name"]:
                        saw_photo = await self._see(client, call.call_id) or saw_photo
                        continue
                    output = run_tool(self._session, call.name, call.arguments, telemetry=self._telemetry)
                    await client.send(ItemCreate.function_call_output(call.call_id, output))
                # Once something was said, the tool results only need to be on
                # record: asking for another response would make it talk twice.
                # A photo is the exception: the model asked to look, so it
                # still has to answer what it saw.
                if calls and (saw_photo or not event.response.has_message):
                    await client.send(ResponseCreate())
            case protocol.ErrorEvent():
                self._raise_if_fatal(event.error)

    async def _see(self, client: RealtimeClientLike, call_id: str) -> bool:
        """Answer a `see` call: the result, then the photo as its own item, since a result is text only.

        Returns:
            bool: Whether a photo was sent.
        """
        frame = self._live_frame
        jpeg = frame.newest() if frame is not None else None
        error = None if jpeg is not None else "no camera frame available"
        output = {"ok": True} if error is None else {"ok": False, "error": error}
        # The result goes first, so the photo follows the call it answers.
        await client.send(ItemCreate.function_call_output(call_id, json.dumps(output)))
        if frame is not None and jpeg is not None:
            await frame.replace(client, jpeg)
        _LOG.info("AI tool call: see() -> %s", "photo sent" if error is None else f"error: {error}")
        self._telemetry.event("tool_call", name="see", args={}, ok=error is None, error=error)
        if error is None:
            self._telemetry.event("frame_push", reason="see")
        return error is None

    def _log_response(self, response: protocol.Response) -> None:
        """Emit one `ai_response` event: what the reply did, how long it took, and what it cost."""
        started = self._response_started.pop(response.id, None)
        usage = response.usage or {}
        input_details = usage.get("input_token_details") or {}
        output_details = usage.get("output_token_details") or {}
        audio_seconds = self._response_audio_bytes / (2 * API_RATE)
        fields: dict[str, Any] = {
            "response_id": response.id,
            "status": response.status,
            "spoke": response.has_message,
            "tools": [call.name for call in response.function_calls],
            "duration_ms": None if started is None else round((self._now() - started) * 1000),
            "audio_out_s": round(audio_seconds, 2),
            "input_tokens": usage.get("input_tokens"),
            "cached_tokens": input_details.get("cached_tokens"),
            "input_audio_tokens": input_details.get("audio_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "output_audio_tokens": output_details.get("audio_tokens"),
            "reasoning_tokens": output_details.get("reasoning_tokens"),
        }
        _LOG.info(
            "AI response %s: %s, spoke=%s, tools=%s, %s ms, %.1f s audio",
            response.id,
            response.status,
            fields["spoke"],
            fields["tools"],
            fields["duration_ms"],
            audio_seconds,
        )
        self._telemetry.event("ai_response", **fields)

    def _raise_if_fatal(self, error: dict[str, Any]) -> None:
        code = error.get("code")
        if code in _QUOTA_CODES:
            raise _AiStopped(NOTICE_QUOTA, str(code))
        if code in _KEY_CODES:
            raise _AiStopped(NOTICE_BAD_KEY, str(code))
        _LOG.warning("Realtime API error: %s", error)


__all__ = ["DEFAULT_MODEL", "AiController", "OpenMic", "PlaybackLike", "ReflexesLike"]
