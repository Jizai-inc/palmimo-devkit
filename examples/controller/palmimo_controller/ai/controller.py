"""AiController: starts and stops one OpenAI Realtime session for an AI mode.

Runs on the server's event loop, the same loop the WebSocket handler writes
`session.PilotSession` from, so the session keeps a single writer. The robot
connection belongs to `robot_loop.RobotLoop` and is never touched here:
switching modes only changes which input the robot loop reads.

Any failure (no key, rejected key, quota, network, a slow start) ends the AI
session, puts the session back in teleop with a plain-language notice, and
leaves the robot connected.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
from collections.abc import Callable, Hashable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from typing import Any, Protocol

from ..modes import PromptSource
from ..session import PilotSession
from ..telemetry import Telemetry
from . import protocol
from .audio import API_RATE, MicrophoneFeed, MicStreamLike
from .client import RealtimeClient, RealtimeClientLike
from .protocol import ItemCreate, ResponseCreate, SessionUpdate
from .tools import TOOL_DEFINITIONS, run_tool


_LOG = logging.getLogger(__name__)

#: Realtime model and voice. Fixed in code: the pilot picks a mode, not a model.
MODEL = "gpt-realtime-2.1"
VOICE = "coral"
LANGUAGE = "ja"

#: Longest a switch to AI may take before it is abandoned (PRD target: 3 s).
START_TIMEOUT_S: float = 8.0

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

#: Realtime `error` codes that mean the key cannot work, so retrying is pointless.
_QUOTA_CODES = frozenset({"insufficient_quota"})
_KEY_CODES = frozenset({"invalid_api_key"})


class _AiStopped(Exception):  # noqa: N818 -- control flow carrying the pilot's notice
    def __init__(self, notice: str, reason: str) -> None:
        super().__init__(reason)
        self.notice = notice
        self.reason = reason


class PlaybackLike(Protocol):
    """The subset of `audio.Playback` the controller needs."""

    def write(self, pcm: bytes) -> None: ...

    def interrupt(self) -> None: ...

    def close(self) -> None: ...


Connect = Callable[..., AbstractAsyncContextManager[RealtimeClientLike]]


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
    """Owns at most one running AI session at a time."""

    def __init__(
        self,
        session: PilotSession,
        prompts: PromptSource,
        *,
        api_key: str | None,
        telemetry: Telemetry,
        mic_factory: Callable[[], AbstractContextManager[MicStreamLike]],
        playback_factory: Callable[[], PlaybackLike],
        connect: Connect = RealtimeClient.connect,
        start_timeout: float = START_TIMEOUT_S,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._prompts = prompts
        self._api_key = api_key or None
        self._telemetry = telemetry
        self._mic_factory = mic_factory
        self._playback_factory = playback_factory
        self._connect = connect
        self._start_timeout = start_timeout
        self._now = now
        self._task: asyncio.Task[None] | None = None

    @property
    def available(self) -> bool:
        """Whether an API key is configured. Teleop never depends on this."""
        return self._api_key is not None

    @property
    def running(self) -> bool:
        """Whether an AI session task is in flight (starting or live)."""
        return self._task is not None and not self._task.done()

    async def start(self, client_id: Hashable, mode_id: str) -> bool:
        """Switch to AI mode *mode_id*.

        Returns:
            bool: `False` if the switch was ignored (not the pilot, or a
                switch already in progress) or failed before connecting.
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
        self._task = asyncio.create_task(self._run(mode_id, prompt, self._now()), name=f"ai-mode-{mode_id}")
        return True

    async def stop(self, *, notice: str | None = None, reason: str = "user") -> None:
        """End the AI session (if any) and return to teleop. Safe to call at any time."""
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

    async def _run(self, mode_id: str, prompt: str, started_at: float) -> None:
        live = False
        playback: PlaybackLike | None = None
        try:
            async with contextlib.AsyncExitStack() as stack:
                # Outside the start timeout: the first open downloads the echo
                # canceller's model files, which is not a slow OpenAI start.
                mic = await self._open_mic(stack)
                async with asyncio.timeout(self._start_timeout):
                    client = await stack.enter_async_context(self._connect(model=MODEL, api_key=self._api_key))
                    await client.send(
                        SessionUpdate(
                            instructions=prompt,
                            tools=TOOL_DEFINITIONS,
                            voice=VOICE,
                            rate=API_RATE,
                            transcription_language=LANGUAGE,
                        )
                    )
                    playback = self._playback_factory()
                self._session.ai_started()
                live = True
                self._telemetry.event(
                    "mode_switch",
                    from_mode="teleop",
                    to_mode=mode_id,
                    ok=True,
                    latency_ms=round((self._now() - started_at) * 1000),
                )
                async with asyncio.TaskGroup() as group:
                    group.create_task(MicrophoneFeed(mic, client).run())
                    group.create_task(self._route(client, playback))
        except Exception as exc:
            # The TaskGroup wraps a service's failure in an ExceptionGroup;
            # the first one is what ended the session.
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
            if playback is not None:
                playback.interrupt()
                await asyncio.to_thread(playback.close)

    async def _open_mic(self, stack: contextlib.AsyncExitStack) -> MicStreamLike:
        """Open the mic on a worker thread and register its close on *stack*.

        Opening blocks (device setup, and a model download on first use), so
        it must not run on the event loop, which also serves every pilot and
        viewer connection. If the switch is cancelled while the open is still
        running, the mic is closed as soon as the open finishes.
        """
        manager = self._mic_factory()
        opening = asyncio.ensure_future(asyncio.to_thread(manager.__enter__))
        try:
            mic = await asyncio.shield(opening)
        except Exception as exc:
            raise _AiStopped(NOTICE_MIC, "mic_unavailable") from exc
        except asyncio.CancelledError:

            def close_when_open(future: asyncio.Future[MicStreamLike]) -> None:
                if not future.cancelled() and future.exception() is None:
                    manager.__exit__(None, None, None)

            opening.add_done_callback(close_when_open)
            raise
        stack.push(manager.__exit__)
        return mic

    async def _route(self, client: RealtimeClientLike, playback: PlaybackLike) -> None:
        async for event in client.events():
            await self._dispatch(client, playback, event)
        raise _AiStopped(NOTICE_NETWORK, "socket_closed")

    async def _dispatch(
        self, client: RealtimeClientLike, playback: PlaybackLike, event: protocol.ServerEvent | protocol.Unknown
    ) -> None:
        match event:
            case protocol.AudioDelta():
                playback.write(base64.b64decode(event.delta))
            case protocol.SpeechStarted():
                # The visitor started talking: stop speaking over them.
                _LOG.info("AI: heard speech")
                playback.interrupt()
            # Logged as lengths only, never the words: visitors did not agree
            # to have what they say kept in the robot's journal.
            case protocol.TranscriptCompleted():
                _LOG.info("AI: visitor speech understood (%d chars)", len(event.transcript))
            case protocol.TranscriptDone():
                _LOG.info("AI: replied with speech (%d chars)", len(event.transcript))
            case protocol.ResponseDone():
                calls = event.response.function_calls
                for call in calls:
                    output = run_tool(self._session, call.name, call.arguments)
                    await client.send(ItemCreate.function_call_output(call.call_id, output))
                if calls:
                    await client.send(ResponseCreate())
            case protocol.ErrorEvent():
                self._raise_if_fatal(event.error)

    def _raise_if_fatal(self, error: dict[str, Any]) -> None:
        code = error.get("code")
        if code in _QUOTA_CODES:
            raise _AiStopped(NOTICE_QUOTA, str(code))
        if code in _KEY_CODES:
            raise _AiStopped(NOTICE_BAD_KEY, str(code))
        _LOG.warning("Realtime API error: %s", error)


__all__ = ["MODEL", "AiController", "PlaybackLike"]
