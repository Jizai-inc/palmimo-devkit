"""Shared tool dispatch -- the cancellable execution both turn types run tool calls through.

:class:`~.idle.IdleTurn` and :class:`~.respond.RespondTurn` share one
per-call pipeline (:func:`execute_and_record`), one cancellation-aware
dispatch underneath it (:func:`run_tool`), and the same turn-start failure
handling (:func:`record_llm_failure`, :func:`record_empty_choices`,
:func:`record_no_tool_call`) -- each turn type keeps only what actually
differs (idle's one-call tick vs. respond's multi-call plan, and each
side's own wording).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any, Protocol

from palmimo_sdk.agent.tools import ToolResult

from .history import AgentThoughtEvent, CameraEvent, SystemNoteEvent, ToolExecEvent


if TYPE_CHECKING:
    from ..core.toolview import ToolView
    from .bus import Bus
    from .history import History
    from .llm import LlmProvider


_log = logging.getLogger(__name__)

#: Prompt handed to the VLM for every image a tool call returns.
_VLM_DESCRIBE_PROMPT = "Briefly describe what the robot's camera currently sees, in one short sentence."

#: Backoff after a failed or empty LLM response before the caller retries.
_LLM_ERROR_BACKOFF_SECONDS = 2.0

_LLM_ERROR_NOTE_PREFIX = "[error] The LLM call failed (retrying shortly): "
_EMPTY_CHOICES_NOTE = "[error] The LLM returned no choices (retrying shortly)."
_EMPTY_RESPONSE_NOTE = "[notice] The LLM returned neither a tool call nor any text."

#: A tool result whose text starts with this reports the call was
#: interrupted (the stale-cancel guard in :func:`run_tool`, or a
#: long-running motion cancelled mid-flight -- see
#: :meth:`~palmimo_sdk.agent.toolset.AgentToolSet.call`'s own
#: ``MotionCancelled`` -> ``"interrupted: ..."`` handling), rather than
#: a normal or failed outcome.
INTERRUPTED_PREFIX = "interrupted"


def is_interrupted_result(text: str) -> bool:
    """Whether *text* reports an interrupted call -- see :data:`INTERRUPTED_PREFIX`."""
    return text.startswith(INTERRUPTED_PREFIX)


class _FunctionLike(Protocol):
    @property
    def name(self) -> str | None: ...
    @property
    def arguments(self) -> str | None: ...


class ToolCallLike(Protocol):
    """Structural surface of a LiteLLM/OpenAI ``tool_calls[i]`` entry this module reads."""

    @property
    def id(self) -> str: ...
    @property
    def function(self) -> _FunctionLike: ...


async def run_tool(view: ToolView, bus: Bus, name: str, args: dict[str, Any]) -> ToolResult:
    """Dispatch *name* through *view*, racing long-running tools against *bus*'s cancel signal.

    A cancel already set when this is entered means an interrupt arrived
    while the LLM call that picked this tool was in flight -- the tool call
    is stale, so it's skipped outright rather than run (and the flag is
    cleared, since this call is the one consuming it). Otherwise this is a
    thin wrapper around :meth:`~palmimo_companion_agent.core.toolview.ToolView.call`
    for a short tool, or :func:`_run_cancellable` for a long-running one --
    validation failures, an unknown/disallowed tool name, and a raised
    exception all come back as an ordinary (non-raising)
    :class:`~palmimo_sdk.agent.tools.ToolResult`, so there is nothing left
    for this function to catch.
    """
    if bus.cancel.is_set():
        bus.cancel.clear()
        return ToolResult(text=INTERRUPTED_PREFIX)
    if view.is_long_running(name):
        return await _run_cancellable(view, bus, name, args)
    return await view.call(name, args)


async def _run_cancellable(view: ToolView, bus: Bus, name: str, args: dict[str, Any]) -> ToolResult:
    """Run a long-running tool call through *view*, racing it against ``bus.cancel``.

    A cancel interrupt winning the race asks the toolset itself to abort the
    in-flight call (:meth:`~palmimo_companion_agent.core.toolview.ToolView.cancel_running`):
    the SDK guarantees the motion stops and the stance settles, and the call
    still returns a :class:`~palmimo_sdk.agent.tools.ToolResult` (an
    ``"interrupted: ..."`` one) rather than raising -- so there is no
    separate "stop" tool call to make and no bespoke hardware cleanup here.
    """
    task: asyncio.Task[ToolResult] = asyncio.ensure_future(view.call(name, args))
    cancel_wait: asyncio.Task[Any] = asyncio.ensure_future(bus.cancel.wait())
    try:
        done, _ = await asyncio.wait({task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        # The enclosing turn itself is being cancelled (shutdown): let the
        # in-flight call settle via the toolset's own cancellation before
        # this unwinds, or the robot is left mid-motion with nothing
        # tracking it.
        await view.cancel_running()
        cancel_wait.cancel()
        with contextlib.suppress(BaseException):
            await task
        with contextlib.suppress(BaseException):
            await cancel_wait
        raise
    if task in done:
        cancel_wait.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_wait
        return task.result()
    # A cancel interrupt won: ask the toolset to abort, then wait for the
    # call to actually finish -- never abandon it, it alone tracks whether
    # the robot has stopped.
    await view.cancel_running()
    bus.cancel.clear()
    await task
    return task.result()


async def describe_images(llm: LlmProvider, history: History, images: list[bytes]) -> None:
    """Describe each image via the VLM, recording a :class:`~.history.CameraEvent` each.

    A description failure is recorded as a fallback note rather than raised
    -- a flaky VLM call must not turn a successful capture into a crashed
    turn.
    """
    for jpeg in images:
        try:
            image_b64 = base64.b64encode(jpeg).decode("ascii")
            description = await llm.describe_image(image_b64, _VLM_DESCRIBE_PROMPT)
        except Exception as exc:
            _log.warning("describe_image failed; recording a fallback note", exc_info=True)
            description = f"saw something, but the description failed: {exc}"
        history.add(CameraEvent(description))


async def execute_and_record(
    view: ToolView,
    bus: Bus,
    llm: LlmProvider,
    history: History,
    tool_call: ToolCallLike,
    *,
    thought: str | None,
) -> bool:
    """Parse, dispatch, and record one planned tool call -- the pipeline both turn types share.

    Parses ``tool_call.function.arguments`` as JSON (gemini flash-lite
    occasionally emits malformed JSON here -- e.g. stray ``")}\")"``
    artifacts -- which becomes an ``"execution error: ..."`` observation
    rather than propagating), dispatches it through :func:`run_tool`,
    records the outcome as a :class:`~.history.ToolExecEvent`, and describes
    any returned image via :func:`describe_images`.

    Args:
        thought: Fallback for the event's ``thought`` when the call's own
            ``reason`` is missing. Idle passes the turn's assistant content
            (its only chance to keep it, one call per tick); respond passes
            None, since a plan's assistant content -- when present -- is
            recorded once as its own :class:`~.history.AgentThoughtEvent`
            instead of attached to every call.

    Returns:
        Whether the result reports it was interrupted (see
        :func:`is_interrupted_result`) -- respond uses this to stop a
        multi-call plan early; idle (one call per tick) ignores it.
    """
    name = tool_call.function.name
    if name is None:
        return False
    arguments = tool_call.function.arguments or "{}"
    try:
        args = json.loads(arguments)
    except ValueError:
        args = None
    reason = args.get("reason") if isinstance(args, dict) else None
    resolved_thought = reason if isinstance(reason, str) and reason else thought

    if not isinstance(args, dict):
        result_text = f"execution error: tool arguments were not a JSON object: {arguments[:200]}"
        images: list[bytes] = []
        result_is_error = True
    else:
        try:
            result = await run_tool(view, bus, name, args)
            result_text, images, result_is_error = result.text, result.images, result.is_error
        except Exception as exc:  # A tool failure is an observation, not a crash.
            result_text, images, result_is_error = f"execution error: {exc}", [], True

    history.add(
        ToolExecEvent(
            tool_call_id=tool_call.id,
            name=name,
            arguments=arguments,
            result=result_text,
            error=result_is_error,
            thought=resolved_thought,
        )
    )
    if images:
        await describe_images(llm, history, images)
    return is_interrupted_result(result_text)


async def record_llm_failure(history: History, exc: Exception) -> None:
    """Record a failed ``chat()`` call and back off before the caller retries.

    A single bad call must not take the whole turn type down: the note is
    visible to the LLM on the next attempt too (which also resolves any
    Gemini function-call-must-follow-user-or-function-turn constraint left
    dangling by the failed turn).
    """
    history.add(SystemNoteEvent(f"{_LLM_ERROR_NOTE_PREFIX}{exc}"))
    await asyncio.sleep(_LLM_ERROR_BACKOFF_SECONDS)


async def record_empty_choices(history: History) -> None:
    """Record a ``chat()`` response with no choices and back off before the caller retries."""
    history.add(SystemNoteEvent(_EMPTY_CHOICES_NOTE))
    await asyncio.sleep(_LLM_ERROR_BACKOFF_SECONDS)


def record_no_tool_call(history: History, content: str | None, *, no_tool_call_note: str) -> None:
    """Record a turn that returned no tool call.

    Any text becomes an :class:`~.history.AgentThoughtEvent` (UI/log-only)
    plus a :class:`~.history.SystemNoteEvent` nudging the model back toward
    the tool-calling contract. The nudge's exact wording is caller-specific
    -- idle's ("call exactly one tool") differs from respond's ("one or
    several, up to 4"), so it is passed in rather than owned here. A fully
    empty reply gets :data:`_EMPTY_RESPONSE_NOTE` instead, regardless of caller.
    """
    stripped = content.strip() if content else ""
    if stripped:
        history.add(AgentThoughtEvent(stripped))
        history.add(SystemNoteEvent(no_tool_call_note))
        return
    history.add(SystemNoteEvent(_EMPTY_RESPONSE_NOTE))


__all__ = [
    "INTERRUPTED_PREFIX",
    "ToolCallLike",
    "describe_images",
    "execute_and_record",
    "is_interrupted_result",
    "record_empty_choices",
    "record_llm_failure",
    "record_no_tool_call",
    "run_tool",
]
