# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Builds the low-level MCP :class:`~mcp.server.lowlevel.Server` that fronts an
:class:`~palmimo_sdk.agent.toolset.AgentToolSet`.

Deliberately uses ``mcp.server.lowlevel.Server`` rather than the higher-level
``MCPServer`` wrapper: :class:`~palmimo_sdk.agent.tools.Tool` already IS a
pydantic model with its own JSON-schema export
(:meth:`~palmimo_sdk.agent.tools.Tool.parameters_schema`), so there is nothing
for ``MCPServer``'s function-signature-to-schema machinery to add -- registering
each tool's already-built schema straight onto the low-level server is more
direct. Handlers are passed as the ``on_list_tools``/``on_call_tool``
constructor kwargs (the MCP SDK 2.0 registration surface -- the 1.x
``@server.list_tools()``/``@server.call_tool()`` decorators were removed).

:meth:`~palmimo_sdk.agent.toolset.AgentToolSet.call` is itself ``async``:
it already serializes overlapping callers against
each other with its own internal lock, and already runs
:meth:`~palmimo_sdk.agent.tools.Tool.execute` -- synchronous and potentially
blocking for seconds (a gesture's ``run(seconds=...)``) against a Palmimo
facade that assumes single-threaded access -- on a worker thread via
``asyncio.to_thread``. :func:`build_mcp_server`'s ``call_tool`` handler wraps
``toolset.call(...)`` in its own :class:`asyncio.Task` and awaits it behind
:func:`asyncio.shield`, so a cancellation of the HANDLER's own task (the MCP
request's task being cancelled or abandoned -- a client disconnect, the
session shutting down, ...) cannot reach into the ``toolset.call()``
coroutine at all; it keeps running, undisturbed, until it actually finishes.
This restores the same non-preemptive guarantee the earlier synchronous
implementation got for free from
``anyio.to_thread.run_sync(..., abandon_on_cancel=False)`` (the default),
now expressed at this layer instead of relying on
:meth:`~palmimo_sdk.agent.toolset.AgentToolSet.call`'s own internal
cancellation handling to happen to produce the same effect.

**This server remains deliberately non-preemptive**: it never calls
:meth:`~palmimo_sdk.agent.toolset.AgentToolSet.cancel_running`. A motion
already in flight cannot be preempted by a ``stop`` call arriving on a
second, concurrent MCP request -- ``stop`` waits on ``toolset``'s own lock
and only runs once the in-flight call releases it. Cancelling or
disconnecting the *waiting* request does not help either: the shield above
means the in-flight call always runs to completion rather than being
abandoned mid-motion. This is deliberate: letting one already-dispatched
call run to completion, rather than trying to cut a synchronous, blocking
robot action off partway through, is what keeps the robot from being left
torqued in an in-between pose (e.g. one leg mid-stride) rather than settled
back onto a stance. A caller that wants a genuinely preemptible tool-calling
loop (e.g. an agent racing a barge-in against a long-running gesture) should
drive :class:`~palmimo_sdk.agent.toolset.AgentToolSet` directly instead of
going through this MCP server.

Every dispatched call leaves exactly one ``INFO`` record on the
``palmimo_sdk.mcp.server`` logger -- see :func:`_log_tool_call` for the
record's shape and :data:`TOOL_CALL_LOG_DETAILS` for how much of a call it
carries. This module only *emits*; it installs no handler and touches no other
logger's configuration, so an embedding application decides where the records
go (:mod:`palmimo_sdk.mcp.__main__` sends them to stderr). That division
matters more here than usual: on the stdio transport, stdout IS the MCP
protocol stream, so a library that attached a stdout handler of its own would
corrupt every session it was imported into.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.metadata
import logging
import time
from typing import TYPE_CHECKING, Any, Final, Literal

import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server

from palmimo_sdk.agent.tools import ToolResult
from palmimo_sdk.agent.toolset import AgentToolSet


if TYPE_CHECKING:
    from collections.abc import Mapping


logger = logging.getLogger(__name__)

# How much of a tool call its INFO record carries.
#
# * ``off`` -- no per-call record at all.
# * ``summary`` -- the default: tool name, argument NAMES with their numeric
#   values, outcome, duration, and the size of the result. Enough to
#   reconstruct what the robot was told to do and what came back, without
#   copying caller-supplied text into a log.
# * ``full`` -- the same record with every argument value and the result text
#   verbatim, for debugging a specific call.
#
# See :func:`_render_value` for exactly what ``summary`` keeps and what it
# elides, and why the split is drawn on the value's TYPE rather than on a list
# of argument names.
TOOL_CALL_LOG_DETAILS: Final[tuple[str, ...]] = ("off", "summary", "full")

ToolCallLogDetail = Literal["off", "summary", "full"]

# The prefix :meth:`~palmimo_sdk.agent.toolset.AgentToolSet.call` puts on the
# two results that mean "this call did not run to completion" -- a
# ``MotionCancelled`` raised out of ``execute()``, and a cancel that landed
# before dispatch. Both come back with ``is_error=False``, deliberately (being
# interrupted is an observation, not a failure), so the text is the only thing
# that separates them from an ordinary success. Matching on it here keeps
# "interrupted" a distinguishable outcome in the log without adding a field to
# ``ToolResult`` that only this logger would read; the coupling is pinned by a
# test that drives a real cancellation through a real ``AgentToolSet``.
_INTERRUPTED_PREFIX = "interrupted: "


def _sdk_version() -> str:
    """The installed ``palmimo-sdk`` distribution version, or ``""`` if unresolvable.

    Best-effort: an editable/unusual install (or a distribution renamed
    downstream) can leave ``importlib.metadata`` unable to find it, in which
    case the server simply reports an unversioned ``""`` -- matching
    :class:`~mcp.server.lowlevel.Server`'s own default -- rather than raising.
    """
    try:
        return importlib.metadata.version("palmimo-sdk")
    except importlib.metadata.PackageNotFoundError:
        return ""


def _render_value(value: Any, *, verbatim: bool) -> str:
    """Render one argument value for the log, eliding it unless *verbatim*.

    The elision rule is drawn on the value's **type**, not on a list of
    argument names known to be harmless:

    * ``None`` and numbers (``int``, ``float``, ``bool``) are always printed.
      Every argument that shapes a physical movement today is one of these --
      ``seconds``, ``pitch``, ``yaw`` -- and they are exactly what a
      reconstruction of "what did it do before it broke" needs. A number
      drawn from a bounded, schema-declared range cannot carry prose, a
      credential, or a person's name.
    * A ``str`` is reduced to its length. Strings are the only shape in which
      caller-authored content reaches a tool today (``say``'s ``text`` is
      spoken verbatim, and comes straight from whoever is driving the client),
      and the only shape a future secret-carrying argument could plausibly
      take.
    * Anything else is elided outright, so a container added by a tool that
      does not exist yet is not printed by default.

    Choosing the type over an allowlist of names is what makes this stay
    correct for tools nobody has written yet: an allowlist would silently
    start printing the first argument someone forgot to add to it, and the
    failure would be invisible until it was already in a log. The cost is
    real and accepted -- ``turn``'s ``direction`` is a closed
    ``Literal["left", "right"]`` that could safely be printed but is elided
    with the rest, so the default record says a turn happened and for how
    long, not which way. ``full`` prints it, and prints everything else too.
    """
    if verbatim or value is None or isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return f"<str, {len(value)} chars>"
    return f"<{type(value).__name__}, elided>"


def _render_arguments(arguments: Mapping[str, Any] | None, *, verbatim: bool) -> str:
    """Render a call's arguments as ``{name=value, ...}``, per :func:`_render_value`.

    Argument *names* are always printed: they come from the tool's own schema,
    never from the caller, so "which knobs were touched" survives even when
    every value is elided.
    """
    if not arguments:
        return "{}"
    return "{" + ", ".join(f"{key}={_render_value(value, verbatim=verbatim)}" for key, value in arguments.items()) + "}"


def _outcome(result: ToolResult) -> str:
    """Classify a finished call as ``ok``, ``error``, or ``interrupted``.

    ``interrupted`` is kept apart from both: it is neither a failure to
    investigate nor a clean success, and a call that was cut short is the one
    thing a reader of these records most needs to be able to tell from a call
    that completed. See :data:`_INTERRUPTED_PREFIX` for how it is recognised.
    """
    if result.is_error:
        return "error"
    if result.text.startswith(_INTERRUPTED_PREFIX):
        return "interrupted"
    return "ok"


def _log_tool_call(
    *,
    name: str,
    arguments: Mapping[str, Any] | None,
    result: ToolResult | None,
    outcome: str,
    elapsed_seconds: float,
    detail: ToolCallLogDetail,
) -> None:
    """Emit the single ``INFO`` record this server keeps for one dispatched call.

    The record is one line of ``key=value`` fields -- ``name``, ``args``,
    ``outcome``, ``duration_ms``, ``result``, ``images`` -- so it can be
    grepped and, in particular, so ``duration_ms`` can be read back
    mechanically: measuring end-to-end latency against a running server
    previously meant writing a separate poller, because the server itself
    could not say when a tool ran.

    *elapsed_seconds* is measured from the moment this handler received the
    request, so it includes any time the call spent queued behind an earlier
    one on :class:`~palmimo_sdk.agent.toolset.AgentToolSet`'s lock. That is
    deliberate: it is the wait a client actually experiences, and this server
    never preempts, so queueing is a normal part of a call's cost rather than
    noise to be subtracted out.

    *result* is ``None`` only when the request was abandoned before its result
    could be returned; the call itself still ran to completion (this server
    never abandons one), so the record still reports its duration.
    """
    if detail == "off":
        return
    verbatim = detail == "full"
    if result is None:
        rendered = "result=<unreturned: the request was cancelled> images=0"
    else:
        text = repr(result.text) if verbatim else f"<str, {len(result.text)} chars>"
        rendered = f"result={text} images={len(result.images)}"
    logger.info(
        "tool call name=%s args=%s outcome=%s duration_ms=%.1f %s",
        name,
        _render_arguments(arguments, verbatim=verbatim),
        outcome,
        elapsed_seconds * 1000.0,
        rendered,
    )


_SERVER_INSTRUCTIONS = (
    "Controls a Palmimo hexapod robot. Timed motions (forward, wave, dance, "
    "...) settle back to a neutral stance on their own once they finish. "
    "Calls are serialized (queued): `stop` does not interrupt a motion "
    "already in flight, it runs after that motion completes -- there is no "
    "way to preempt one call from another. Peripherals (servo bus, face "
    "display, speaker, head camera) that were not reachable at startup have "
    "their tools respond with a descriptive not-attached message rather than "
    "being hidden from the tool list."
)


def build_mcp_server(toolset: AgentToolSet, log_tool_calls: ToolCallLogDetail = "summary") -> Server[Any]:
    """Build an MCP server that lists and dispatches *toolset*'s tools.

    Args:
        toolset: The tool registry/dispatcher to expose. :func:`list_tools`
            re-reads :attr:`~palmimo_sdk.agent.toolset.AgentToolSet.tool_models`
            on every request, so a :meth:`~palmimo_sdk.agent.toolset.AgentToolSet.register`
            call made after the server was built is reflected the next time a
            client actually asks -- but MCP has no ``tools/list_changed``
            notification wired up here (the server does not advertise the
            ``listChanged`` capability), so a client that caches its first
            ``tools/list`` response from initialization will not learn about
            the change on its own.
        log_tool_calls: How much of each dispatched call to record on the
            ``palmimo_sdk.mcp.server`` logger -- one of
            :data:`TOOL_CALL_LOG_DETAILS`. Defaults to ``"summary"``: this is
            a remote interface to a machine that physically moves, so a call
            leaving no trace at all is the wrong default. Emitting is free
            when nobody is listening -- the record only goes somewhere once an
            application configures a handler for that logger.

    Returns:
        A :class:`~mcp.server.lowlevel.Server` ready to ``run()`` over any MCP
        transport (stdio, streamable HTTP, ...). Building the server does not
        connect the robot or start serving -- see :mod:`palmimo_sdk.mcp.__main__`
        for a ready-to-run CLI that does both.
    """

    async def list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(name=name, description=cls.description, input_schema=cls.parameters_schema())
                for name, cls in toolset.tool_models.items()
            ]
        )

    async def call_tool(ctx: ServerRequestContext[Any], params: types.CallToolRequestParams) -> types.CallToolResult:
        # Run toolset.call() as its own Task and await it behind a shield, so
        # a cancellation of THIS handler's own task (the MCP request being
        # cancelled/abandoned) cannot reach into toolset.call() at all -- it
        # keeps running to completion regardless. See the module docstring
        # for why this non-preemptive guarantee matters (the robot must
        # never be abandoned torqued mid-motion). If the handler's task IS
        # cancelled while waiting, this loops on the shield (suppressing
        # CancelledError each time) until the worker is actually done, then
        # re-raises so the cancellation still propagates -- just only after
        # the call has settled.
        started = time.monotonic()
        task: asyncio.Task[ToolResult] = asyncio.ensure_future(toolset.call(params.name, params.arguments or {}))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(task)
            # Recorded on the way out, not skipped: an abandoned request is
            # precisely the case where the client has no result to tell anyone
            # about, so the server's own record is the only evidence the call
            # ran at all -- and it did run, to completion, behind the shield.
            _log_tool_call(
                name=params.name,
                arguments=params.arguments,
                result=None,
                outcome="interrupted",
                elapsed_seconds=time.monotonic() - started,
                detail=log_tool_calls,
            )
            raise
        _log_tool_call(
            name=params.name,
            arguments=params.arguments,
            result=result,
            outcome=_outcome(result),
            elapsed_seconds=time.monotonic() - started,
            detail=log_tool_calls,
        )

        # toolset.call() never raises (unknown tool / bad args / execute()-time
        # error all come back as an explanatory ToolResult instead) -- so
        # there is no path here that needs to raise an MCP protocol/tool
        # error; every outcome, including "unknown tool", is reported as
        # ordinary TextContent. What DOES vary is ToolResult.is_error, which
        # toolset.call() sets on exactly those three failure paths (and
        # otherwise leaves at whatever the Tool's own execute() returned,
        # normally False) -- see AgentToolSet.call's docstring. That maps
        # straight onto CallToolResult.is_error below, so an MCP client can
        # tell "the call failed" from "the call succeeded and reported a
        # mundane outcome" (e.g. "no camera attached") without parsing text.

        # Text first, images after -- mirrors ToolResult.to_openai_messages'
        # ordering (the observation text, then any pictures to look at).
        content: list[types.ContentBlock] = [types.TextContent(type="text", text=result.text)]
        content.extend(
            types.ImageContent(type="image", data=base64.b64encode(jpeg).decode("ascii"), mime_type="image/jpeg")
            for jpeg in result.images
        )
        return types.CallToolResult(content=content, is_error=result.is_error)

    return Server(
        "palmimo",
        version=_sdk_version(),
        instructions=_SERVER_INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
