"""AiController: runs one AI mode at a time as a listen → transcribe → chat → speak loop.

Speech is cut into utterances on the robot (Silero VAD), transcribed and
answered over the OpenAI API, and spoken locally with Piper. A reply's tool
calls become motion intents in `session.PilotSession`.

Runs on the server's event loop, the same loop the WebSocket handler writes
`session.PilotSession` from, so the session keeps a single writer. The robot
connection belongs to `robot_loop.RobotLoop` and is never touched here:
switching modes only changes which input the robot loop reads.

A failure that retrying cannot fix (no key, rejected key, quota, mic, voice)
ends the mode, puts the session back in teleop with a plain-language notice,
and leaves the robot connected. A turn that fails on the network is dropped
and the mode keeps listening, unless several fail in a row.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
from collections.abc import Callable, Hashable
from contextlib import AbstractContextManager
from typing import Any, NamedTuple, Protocol, TypeVar

from palmimo_sdk.audio import int16_to_wav

from ..modes import PromptSource
from ..session import PilotSession
from ..telemetry import Telemetry
from .audio import Listener, MicStreamLike, SegmenterLike, SpeakerLike, Voice
from .filters import is_noise
from .llm import LlmError, LlmLike
from .tools import TOOL_DEFINITIONS, run_tool
from .vad import SAMPLE_RATE, Int16Array


_LOG = logging.getLogger(__name__)

#: Longest the API check at a switch to AI may take before it is abandoned.
START_TIMEOUT_S: float = 8.0

#: Chat calls per utterance. The last one is offered no tools, so it has to answer in words.
MAX_CHAT_ROUNDS = 3

#: Exchanges kept as context. Older ones are dropped; the whole history is dropped when the mode ends.
HISTORY_TURNS = 8

#: Network failures in a row that end the mode rather than dropping one more turn.
MAX_TRANSIENT_FAILURES = 3

#: Utterances waiting for a reply. When full, the oldest is dropped: the newest is what the visitor waits on.
_UTTERANCE_QUEUE_MAX = 3

# Messages the pilot sees when AI stops. Shown on the pilot page as-is.
NOTICE_NO_KEY = "AIは使用できません：ポータルでAPIキーを設定してください"
NOTICE_BAD_KEY = "AIは使用できません：ポータルでAPIキーを確認してください"
NOTICE_QUOTA = "AIは使用できません：OpenAIの利用上限に達しました"
NOTICE_NETWORK = "AIとの接続が切れました。手動操作に戻りました"
NOTICE_TIMEOUT = "AIの起動に時間がかかりすぎました。もう一度お試しください"
NOTICE_NO_PROMPT = "このモードは読み込めませんでした"
NOTICE_MIC = "マイクを開けませんでした。手動操作に戻りました"
NOTICE_VOICE = "音声の準備ができませんでした。手動操作に戻りました"
NOTICE_ROBOT_OFFLINE = "ロボットとの接続に問題があるため、AIを停止しました"
NOTICE_FAILED = "AIでエラーが発生しました。手動操作に戻りました"

_NOTICE_FOR_KIND = {"auth": NOTICE_BAD_KEY, "quota": NOTICE_QUOTA, "model": NOTICE_FAILED, "transient": NOTICE_NETWORK}

_T = TypeVar("_T")


class OpenMic(NamedTuple):
    """An open microphone, and whether its capture cancels the robot's own voice."""

    stream: MicStreamLike
    echo_cancelled: bool


class OpenVoice(NamedTuple):
    """A loaded speech synthesizer and utterance segmenter."""

    speaker: SpeakerLike
    segmenter: SegmenterLike


class ReflexesLike(Protocol):
    """The part of `reflexes.Reflexes` the controller runs."""

    async def run(self) -> None: ...


class _AiStopped(Exception):  # noqa: N818 -- control flow carrying the pilot's notice
    def __init__(self, notice: str, reason: str) -> None:
        super().__init__(reason)
        self.notice = notice
        self.reason = reason


def _classify(exc: BaseException) -> tuple[str, str]:
    """Map a failure to `(notice for the pilot, short reason for telemetry)`."""
    if isinstance(exc, _AiStopped):
        return exc.notice, exc.reason
    if isinstance(exc, LlmError):
        return _NOTICE_FOR_KIND[exc.kind], str(exc)
    if isinstance(exc, TimeoutError):
        return NOTICE_TIMEOUT, "start_timeout"
    return NOTICE_FAILED, type(exc).__name__


class History:
    """The mode's prompt plus its last few exchanges, in Chat Completions message form.

    An exchange is a visitor's utterance and everything said and done in reply,
    dropped as a unit so a tool result never outlives the call it answers.
    """

    def __init__(self, prompt: str, *, max_turns: int = HISTORY_TURNS) -> None:
        self._system = {"role": "system", "content": prompt}
        self._turns: collections.deque[list[dict[str, Any]]] = collections.deque(maxlen=max_turns)

    def begin(self, text: str) -> None:
        self._turns.append([{"role": "user", "content": text}])

    def add(self, message: dict[str, Any]) -> None:
        self._turns[-1].append(message)

    def messages(self) -> list[dict[str, Any]]:
        return [self._system, *(message for turn in self._turns for message in turn)]


def _offer(queue: asyncio.Queue[Int16Array], utterance: Int16Array) -> None:
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(utterance)


class AiController:
    """Owns at most one running AI mode at a time."""

    def __init__(
        self,
        session: PilotSession,
        prompts: PromptSource,
        *,
        llm: LlmLike | None,
        telemetry: Telemetry,
        mic_factory: Callable[[], AbstractContextManager[OpenMic]],
        voice_factory: Callable[[], AbstractContextManager[OpenVoice]],
        start_timeout: float = START_TIMEOUT_S,
        now: Callable[[], float] = time.monotonic,
        reflexes_factory: Callable[[], ReflexesLike] | None = None,
    ) -> None:
        self._session = session
        self._prompts = prompts
        self._llm = llm
        self._telemetry = telemetry
        self._mic_factory = mic_factory
        self._voice_factory = voice_factory
        self._start_timeout = start_timeout
        self._now = now
        self._reflexes_factory = reflexes_factory
        self._task: asyncio.Task[None] | None = None

    @property
    def available(self) -> bool:
        """Whether an API key is configured. Teleop never depends on this."""
        return self._llm is not None

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
        if self._llm is None:
            self._fail(previous, mode_id, NOTICE_NO_KEY, "no_api_key", latency_ms=0)
            return False
        try:
            prompt = self._prompts.prompt(mode_id)
        except Exception:
            _LOG.exception("could not load prompt for mode %r", mode_id)
            self._fail(previous, mode_id, NOTICE_NO_PROMPT, "prompt_unavailable", latency_ms=0)
            return False
        self._task = asyncio.create_task(self._run(self._llm, mode_id, prompt, self._now()), name=f"ai-mode-{mode_id}")
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

    async def _run(self, llm: LlmLike, mode_id: str, prompt: str, started_at: float) -> None:
        live = False
        voice: Voice | None = None
        try:
            async with contextlib.AsyncExitStack() as stack:
                # Outside the start timeout: the first open of each downloads
                # model files, which is not a slow OpenAI start.
                mic = await self._enter(stack, self._mic_factory(), NOTICE_MIC, "mic_unavailable")
                loaded = await self._enter(stack, self._voice_factory(), NOTICE_VOICE, "voice_unavailable")
                voice = Voice(loaded.speaker)
                async with asyncio.timeout(self._start_timeout):
                    await llm.check()
                self._session.ai_started()
                live = True
                self._telemetry.event(
                    "mode_switch",
                    from_mode="teleop",
                    to_mode=mode_id,
                    ok=True,
                    latency_ms=round((self._now() - started_at) * 1000),
                )
                utterances: asyncio.Queue[Int16Array] = asyncio.Queue(maxsize=_UTTERANCE_QUEUE_MAX)
                loop = asyncio.get_running_loop()

                def heard(utterance: Int16Array) -> None:
                    with contextlib.suppress(RuntimeError):  # the loop is already closed on shutdown
                        loop.call_soon_threadsafe(_offer, utterances, utterance)

                listener = Listener(mic.stream, loaded.segmenter, voice, heard, barge_in=mic.echo_cancelled)
                # Runs before the mic closes (the stack unwinds in reverse), so the thread exits first.
                stack.callback(listener.stop)
                async with asyncio.TaskGroup() as group:
                    group.create_task(asyncio.to_thread(listener.run))
                    group.create_task(self._converse(llm, utterances, voice, History(prompt)))
                    if self._reflexes_factory is not None:
                        # Reflexes.run() only ever ends by cancellation, so it
                        # cannot take the mode down with it.
                        group.create_task(self._reflexes_factory().run())
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
            if voice is not None:
                voice.interrupt()

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

    async def _converse(
        self, llm: LlmLike, utterances: asyncio.Queue[Int16Array], voice: Voice, history: History
    ) -> None:
        failures = 0
        while True:
            utterance = await utterances.get()
            try:
                await self._answer(llm, utterance, voice, history)
            except LlmError as exc:
                if exc.kind != "transient":
                    raise
                failures += 1
                _LOG.warning("AI: turn dropped (%s, %d in a row)", exc, failures)
                self._telemetry.event("ai_turn_failed", error=str(exc), in_a_row=failures)
                if failures >= MAX_TRANSIENT_FAILURES:
                    raise
            else:
                failures = 0

    async def _answer(self, llm: LlmLike, utterance: Int16Array, voice: Voice, history: History) -> None:
        text = await llm.transcribe(int16_to_wav(utterance, SAMPLE_RATE))
        # Logged as lengths only, never the words: visitors did not agree
        # to have what they say kept in the robot's journal.
        if is_noise(text):
            _LOG.info("AI: ignored noise (%d chars)", len(text))
            return
        _LOG.info("AI: visitor speech understood (%d chars)", len(text))
        history.begin(text)
        for round_ in range(MAX_CHAT_ROUNDS):
            tools = TOOL_DEFINITIONS if round_ < MAX_CHAT_ROUNDS - 1 else None
            reply = await llm.chat(history.messages(), tools=tools)
            history.add(reply.message)
            if reply.text:
                voice.say(reply.text)
                _LOG.info("AI: replied with speech (%d chars)", len(reply.text))
            for call in reply.tool_calls:
                output = run_tool(self._session, call.name, call.arguments)
                history.add({"role": "tool", "tool_call_id": call.id, "content": output})
            # Once something was said, the tool results only need to be on
            # record: asking the model again would make it talk twice.
            if reply.text or not reply.tool_calls:
                return


__all__ = ["AiController", "History", "OpenMic", "OpenVoice", "ReflexesLike"]
