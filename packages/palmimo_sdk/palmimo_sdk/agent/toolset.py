# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""AgentToolSet — registry + dispatcher bridging :mod:`palmimo_sdk.agent.tools`
to an LLM tool-calling API.

Provider tool-calling APIs want two things: a JSON-ish list of tool schemas
to hand the model, and a way to turn the model's "call tool X with these
args" back into a real action. :class:`AgentToolSet` owns both:
:meth:`to_openai_tools` renders the registered
:class:`~palmimo_sdk.agent.tools.Tool` models as provider-shaped schemas
(the OpenAI shape, which most tool-calling stacks accept or can convert
from), and :meth:`call` validates + executes one call against the facade
instance the toolset was built with.

``call`` deliberately never raises for the failure modes an LLM can cause on
its own (unknown tool name, bad/missing arguments, an execute()-time error) --
it reports them as an explanatory :class:`ToolResult` instead, the same way a
shell command's stderr would, so a tool-calling loop can read the message and
retry/self-correct instead of crashing. Those three paths also set
:attr:`~palmimo_sdk.agent.tools.ToolResult.is_error`, so a caller that wants
to distinguish "the call itself failed" from "the call succeeded and reported
a mundane outcome" (e.g. an MCP client rendering ``CallToolResult.is_error``)
still can, without ``call`` having to raise to signal it.

``call`` is ``async``: it serializes overlapping callers
against each other with an internal :class:`asyncio.Lock`, then runs
:meth:`~palmimo_sdk.agent.tools.Tool.execute` -- synchronous and potentially
blocking for seconds, e.g. a gesture's ``run(seconds=...)`` -- on a worker
thread via :func:`asyncio.to_thread`, so the event loop stays free for other
async work (a barge-in watcher, another MCP request, ...) while a motion is
in flight. See :meth:`AgentToolSet.call` for the full contract, including how
:class:`~palmimo_sdk.robot.MotionCancelled` and the calling task's own
cancellation are handled.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from palmimo_sdk.robot import MotionCancelled

from .receiver import PalmimoLike
from .tools import (
    BackwardTool,
    BodyTiltTool,
    BowTool,
    CaptureTool,
    ClapTool,
    CreepTool,
    DanceTool,
    ForwardTool,
    HeadShakeTool,
    LookCenterTool,
    LookTool,
    NodTool,
    PushupTool,
    SayTool,
    SetFaceTool,
    ShowEmojiTool,
    SleepTool,
    StopTool,
    StrafeTool,
    StretchTool,
    Tool,
    ToolResult,
    TurnTool,
    WakeTool,
    WaveBothTool,
    WaveTool,
)


# Every built-in tool, keyed by its LLM-facing name. The single source of
# truth for "what ships with the SDK" -- AgentToolSet.__init__ starts from a
# copy of this and narrows/extends it per instance via include/exclude/register.
#
# When a new motion lands on the Palmimo facade, give it a Tool subclass in
# tools.py and register it here (doc/guides/motion-development-guide.md Step 5) --
# or leave a comment below explaining why it is deliberately not exposed.
TOOL_MODELS: dict[str, type[Tool]] = {
    cls.name: cls
    for cls in (
        ForwardTool,
        BackwardTool,
        TurnTool,
        StrafeTool,
        CreepTool,
        DanceTool,
        BodyTiltTool,
        PushupTool,
        WaveTool,
        WaveBothTool,
        ClapTool,
        BowTool,
        StretchTool,
        NodTool,
        HeadShakeTool,
        SleepTool,
        WakeTool,
        LookTool,
        LookCenterTool,
        SetFaceTool,
        ShowEmojiTool,
        SayTool,
        CaptureTool,
        StopTool,
    )
}


class AgentToolSet:
    """A subscribed set of :class:`Tool`\\ s bound to one :class:`Palmimo`.

    Args:
        robot (PalmimoLike): The facade (or a structural stand-in -- see
            :class:`~palmimo_sdk.agent.receiver.PalmimoLike`) every tool's
            :meth:`~palmimo_sdk.agent.tools.Tool.execute`
            runs against. Owned by the caller -- the toolset never connects /
            disconnects it.
        include (iterable of str, optional): If given, only these tool names are registered (an unknown name
            raises :class:`ValueError`). Mutually composable with *exclude*
            (include narrows first, exclude then removes from that).
        exclude (iterable of str, optional): If given, these tool names are dropped from the registered set (an
            unknown name raises :class:`ValueError`).

    **Single-event-loop instances only**: the internal :class:`asyncio.Lock`
    (:attr:`_call_lock`, used by :meth:`call` to serialize overlapping
    callers) binds itself to whichever event loop is running the first time
    it is contended, so one :class:`AgentToolSet` instance must be driven from
    one event loop for its whole lifetime. A process that tears down its loop
    and starts a new one (e.g. between test cases each opening their own
    ``asyncio.run``) must build a fresh :class:`AgentToolSet` for the new
    loop rather than reusing the old instance.
    """

    def __init__(
        self,
        robot: PalmimoLike,
        *,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
    ) -> None:
        self._robot = robot
        self._tools: dict[str, type[Tool]] = dict(TOOL_MODELS)
        # Serializes overlapping call() invocations against each other --
        # the Palmimo facade assumes single-threaded access, one call's
        # execute() at a time. is_busy() reports whether this is currently
        # held (see is_busy()'s docstring for why that -- and not some
        # separate "am I busy" bookkeeping -- is the right signal).
        self._call_lock = asyncio.Lock()
        # Set by cancel_running() for the DURATION of the call it targeted
        # (reset to False at the start of every call()); see call()'s
        # docstring for the stale-cancel window this closes.
        self._cancel_requested = False
        if include is not None:
            include_set = set(include)
            unknown = include_set - self._tools.keys()
            if unknown:
                raise ValueError(
                    f"Unknown tool name(s) in include=: {sorted(unknown)}. Available: {sorted(TOOL_MODELS)}"
                )
            self._tools = {name: cls for name, cls in self._tools.items() if name in include_set}
        if exclude is not None:
            exclude_set = set(exclude)
            unknown = exclude_set - TOOL_MODELS.keys()
            if unknown:
                raise ValueError(
                    f"Unknown tool name(s) in exclude=: {sorted(unknown)}. Available: {sorted(TOOL_MODELS)}"
                )
            self._tools = {name: cls for name, cls in self._tools.items() if name not in exclude_set}

    def register(self, tool_cls: type[Tool]) -> None:
        """Register a custom :class:`Tool` subclass, making it callable/listable.

        Lets a caller extend the toolset with app-specific actions beyond the
        built-ins in :data:`TOOL_MODELS`. Registering a name that is already
        present (built-in or previously registered) replaces it.
        """
        self._tools[tool_cls.name] = tool_cls

    @property
    def tool_names(self) -> list[str]:
        """Names of the tools currently registered, in registration order."""
        return list(self._tools)

    @property
    def tool_models(self) -> Mapping[str, type[Tool]]:
        """Read-only view of the :class:`Tool` classes currently registered, keyed by name.

        Lets a caller build tool schemas straight from each :class:`Tool`
        subclass (e.g. :func:`palmimo_sdk.mcp.build_mcp_server` needs
        ``description`` / :meth:`~palmimo_sdk.agent.tools.Tool.parameters_schema`
        per tool, which :meth:`to_openai_tools` bakes into an OpenAI-shaped dict
        instead of exposing raw) without reaching into the private ``_tools``
        dict. A :class:`~types.MappingProxyType` view over the live dict, so it
        stays current across :meth:`register` calls instead of being a snapshot.
        """
        return MappingProxyType(self._tools)

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """This toolset's tools as OpenAI ``tools=`` entries."""
        return [
            {
                "type": "function",
                "function": {
                    "name": cls.name,
                    "description": cls.description,
                    "parameters": cls.parameters_schema(),
                },
            }
            for cls in self._tools.values()
        ]

    def is_busy(self) -> bool:
        """Whether a :meth:`call` is currently in flight (its internal lock is held).

        The only honest signal of "busy" here is the lock itself: it is held
        for :meth:`call`'s entire body, from the moment it starts serializing
        against other callers to the moment it returns/raises -- exactly the
        span during which a :meth:`cancel_running` makes sense to issue.
        """
        return self._call_lock.locked()

    async def cancel_running(self) -> None:
        """Ask the in-flight :meth:`call` (if any) to abort, and stop any
        in-flight speech -- unconditionally, so both sides of a barge-in
        (interrupting motion AND interrupting speech) go through one call.

        Motion cancellation is gated on :meth:`is_busy` -- a no-op when
        idle, there being nothing to cancel. When busy, this does two
        things: (1) calls :meth:`~palmimo_sdk.robot.Palmimo.cancel`, which
        is cross-thread safe (see its docstring) and will raise
        :class:`~palmimo_sdk.robot.MotionCancelled` inside the worker
        thread's ``execute()`` if it has already reached a paced
        ``run()``/``perform_dance()``/``play_realtime()`` call; and (2) sets
        a flag :meth:`call` itself checks (see :meth:`call`'s docstring for
        why this second part exists -- ``Palmimo.cancel()`` alone has a
        window where an early cancel is silently absorbed rather than
        raised).

        Speech cancellation (:meth:`~palmimo_sdk.robot.Palmimo.stop_speech`)
        is called regardless of :meth:`is_busy`: :class:`~.tools.SayTool`
        only joins its background :class:`~palmimo_sdk.io.speaker.SpeechHandle`
        for a brief grace period before returning "speaking (in background)",
        so an utterance is very often still playing with no tool call in
        flight at all -- exactly the case a barge-in needs to interrupt.
        ``stop_speech`` is idempotent and a no-op when nothing is speaking.
        """
        self._robot.stop_speech()
        if self.is_busy():
            self._cancel_requested = True
            self._robot.cancel()

    async def call(self, name: str, args: dict[str, Any] | str) -> ToolResult:
        """Validate and execute one tool call, never raising for an LLM-caused failure.

        *args* is either a dict of arguments or a JSON string of one (most
        tool-calling APIs hand back function arguments as a JSON string, so
        callers do not need to parse it themselves first). An unknown tool
        name, malformed JSON, a pydantic validation failure, or an exception
        raised by :meth:`Tool.execute` are all reported as an explanatory
        :class:`~palmimo_sdk.agent.tools.ToolResult` (with ``is_error=True``)
        rather than propagated, so a driving LLM can read the failure and
        self-correct. A successful ``execute()`` -- including one that itself
        reports a descriptive non-fatal outcome, like "no camera attached" --
        is returned as-is, with whatever ``is_error`` that :class:`Tool` set
        (normally the default ``False``; see :class:`~palmimo_sdk.agent.tools.ToolResult`).

        Overlapping callers are serialized by an internal :class:`asyncio.Lock`
        (see :meth:`is_busy`); once validated, ``execute()`` itself -- synchronous
        and potentially blocking for seconds -- runs on a worker thread via
        :func:`asyncio.to_thread`, so the event loop is free for other async
        work (another coroutine calling :meth:`cancel_running`, an MCP
        server's other protocol traffic, ...) while a motion is in flight.

        :class:`~palmimo_sdk.robot.MotionCancelled` -- raised by ``execute()``
        when :meth:`~palmimo_sdk.robot.Palmimo.cancel` interrupted an
        in-flight ``run()`` -- is special-cased rather than folded into the
        generic "execute() raised" path: it is an observation (the motion was
        interrupted, and :func:`~palmimo_sdk.agent.tools._run_motion`'s
        ``try/finally`` has already ``stop()``-ed the robot), not an error, so
        it comes back as ``ToolResult(text="interrupted: ...", is_error=False)``.

        The calling asyncio task's OWN :class:`asyncio.CancelledError` (a
        timeout, the enclosing task group shutting down, ...) is deliberately
        NOT swallowed by the never-raises contract above -- but it is
        re-raised only after :meth:`~palmimo_sdk.robot.Palmimo.cancel` has
        been issued and the worker thread has actually finished (shield-looped
        until ``task.done()``, so even repeated cancellations cannot abandon
        it), because unwinding while the robot keeps moving underneath would
        leave it mid-motion with nothing tracking it.

        Cancellation coverage is gapless across the call: a
        :meth:`cancel_running` before dispatch is caught by a flag check
        (after one ``await asyncio.sleep(0)``, so a request scheduled in the
        same event-loop turn is seen) and returns "interrupted" without
        executing at all; one landing between dispatch and the worker
        thread's paced entry is caught because ``call()`` arms a cancel
        scope on the facade around the dispatch
        (:meth:`~palmimo_sdk.robot.Palmimo._arm_cancel_scope`, disarmed in a
        ``finally`` so it never leaks into a later call); one landing
        mid-motion raises ``MotionCancelled`` at the next frame boundary.
        """
        async with self._call_lock:
            self._cancel_requested = False

            cls = self._tools.get(name)
            if cls is None:
                available = ", ".join(sorted(self._tools)) or "(none)"
                return ToolResult(text=f"Unknown tool {name!r}. Available tools: {available}", is_error=True)

            if isinstance(args, str):
                try:
                    parsed: dict[str, Any] = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError as exc:
                    return ToolResult(text=f"Invalid JSON arguments for tool {name!r}: {exc}", is_error=True)
            else:
                parsed = args

            try:
                tool = cls.model_validate(parsed)
            except ValidationError as exc:
                return ToolResult(text=f"Invalid arguments for tool {name!r}: {exc}", is_error=True)

            # One deliberate checkpoint before dispatch -- see the
            # "stale-cancel window" section of this docstring.
            await asyncio.sleep(0)
            if self._cancel_requested:
                return ToolResult(
                    text=f"interrupted: tool {name!r} was cancelled before it started executing", is_error=False
                )

            # Arm the dispatch-to-entry cancel window closed (see the
            # "Dispatch-to-entry cancel window" section above), and
            # unconditionally disarm once the worker has actually finished --
            # whether it consumed the armed value or not.
            self._robot._arm_cancel_scope()
            try:
                task: asyncio.Task[ToolResult] = asyncio.ensure_future(asyncio.to_thread(tool.execute, self._robot))
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    # This coroutine (not the tool call) was cancelled -- the
                    # worker thread may still be mid-motion. Signal it to
                    # abort, then wait for it to actually finish (so the
                    # robot is stopped and settled before this coroutine
                    # unwinds) no matter how many further cancellations of
                    # THIS coroutine arrive while we wait: each one only
                    # re-raises CancelledError out of `asyncio.shield`, which
                    # the loop below suppresses and retries, never returning
                    # early and abandoning the worker.
                    self._robot.cancel()
                    while not task.done():
                        with contextlib.suppress(asyncio.CancelledError):
                            await asyncio.shield(task)
                    with contextlib.suppress(BaseException):
                        task.result()
                    raise
                except MotionCancelled as exc:
                    return ToolResult(text=f"interrupted: {exc}", is_error=False)
                except Exception as exc:  # a bad tool must not crash the agent loop
                    return ToolResult(text=f"Tool {name!r} raised an error while executing: {exc}", is_error=True)
                return result
            finally:
                self._robot._disarm_cancel_scope()
