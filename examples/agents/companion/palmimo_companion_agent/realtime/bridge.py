"""ToolBridge -- turns a finished Realtime ``response`` into tool calls, and back into wire events.

Owns the respond :class:`~palmimo_companion_agent.core.toolview.ToolView`, the
set of detached plan-execution tasks, the barge-in epoch counter, this
runtime's own :class:`~.state.Sleeping` mirror, and the rules for what gets
sent back to the API after a plan runs (function call outputs, images, and
the "should this turn get a spoken continuation" decision).

Handles three failure modes a plan's function calls can hit mid-flight:

- **Mid-plan abandonment:** a barge-in bumps an epoch counter, snapshot
  SYNCHRONOUSLY in :meth:`~ToolBridge.handle` -- before the plan is even
  detached as a task -- so a barge-in already buffered behind this
  ``response.done`` (i.e. one whose :meth:`~ToolBridge.interrupt` ran between
  the model deciding this plan and the router handing it to :meth:`handle`)
  aborts the plan before it ever starts, rather than racing the task's own
  first scheduled turn on the event loop to bump the epoch first. Before
  running EACH call in a plan, and after EACH call finishes, the epoch is
  checked against that snapshot; if it moved, every remaining call is
  answered with an "interrupted" function_call_output (so the model is not
  left waiting on a call_id that never resolves) and the plan stops there.
  The epoch is checked ONE more time, after the last call and before a
  speak-after continuation would be sent, so a barge-in landing during the
  final call does not start a response that would compete with whatever the
  barge-in is about to trigger.
- **Idle attribution via metadata:** an idle tick is tagged with
  ``response.metadata == {"palmimo_turn": "idle"}`` (see
  ``services/idle.py``) rather than a claim/owns response-id bookkeeping
  scheme -- simpler, and immune to a response id arriving out of order. An
  idle tick runs only its first call; every other call_id in that response
  still gets a "skipped" function_call_output rather than being left to
  dangle -- the same "no call_id goes unanswered" contract the mid-plan
  abandonment handling keeps.
- **Cancelled responses don't execute:** a response whose
  ``status == "cancelled"`` (the API's own answer to a barge-in landing
  mid-generation) has its function calls resolved with an "interrupted"
  output, never executed -- the calls were never really decided on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import Iterable

from palmimo_sdk.agent.toolset import AgentToolSet

from ..core.toolview import ToolView
from .client import RealtimeClientLike
from .log import EventLog
from .protocol import FunctionCallItem, ItemCreate, Response, ResponseCreate
from .services.idle import IDLE_TURN_METADATA
from .state import Sleeping


#: How AgentToolSet.call reports a motion cancelled part-way -- deliberately
#: not an error result (see its own docstring), so "not is_error" alone
#: counts an interrupted tool as one that finished.
_INTERRUPTED_PREFIX = "interrupted:"

#: Sent back for every function call this bridge decides NOT to run --
#: either because a barge-in bumped the epoch mid-plan, or because the
#: response that carried them was itself cancelled by the API.
_INTERRUPTED_BY_BARGE_IN = "interrupted: cancelled by barge-in"
_INTERRUPTED_BY_CANCELLED_RESPONSE = "interrupted: response cancelled by barge-in"

#: Sent back for every call an idle tick's own truncation drops (everything
#: after the first) -- NOT a barge-in interruption, so it gets its own
#: message rather than being folded into _INTERRUPTED_BY_BARGE_IN.
_SKIPPED_IDLE_TAIL = "skipped: an idle tick does exactly one thing"


class ToolBridge:
    """Executes a response's tool calls against the shared toolset, and reports the outcome."""

    def __init__(self, client: RealtimeClientLike, toolset: AgentToolSet, sleeping: Sleeping, log: EventLog) -> None:
        self._client = client
        # squash_say: the model speaks with its own voice, so `say` must not
        # exist as an argument at all -- see ToolView's own docstring.
        self._view = ToolView(toolset, squash_say=True)
        self._sleeping = sleeping
        self._log = log
        self._epoch = 0
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def view(self) -> ToolView:
        """The respond :class:`~palmimo_companion_agent.core.toolview.ToolView` this bridge dispatches through."""
        return self._view

    @property
    def tasks(self) -> frozenset[asyncio.Task[None]]:
        """Every plan-execution task currently in flight."""
        return frozenset(self._tasks)

    async def interrupt(self) -> None:
        """Barge-in entry point: bump the epoch and cancel whatever the toolset is running.

        Called from :class:`~.services.router.BargeIn`. Bumping the epoch
        happens BEFORE the cancel so a plan loop's next epoch check (which
        may already be waiting on the event loop) sees the new value.
        """
        self._epoch += 1
        await self._view.cancel_running()

    async def handle(self, response: Response) -> None:
        """React to a finished response: F3 for a cancelled one, otherwise detach a plan run.

        The epoch is snapshotted HERE, synchronously, before the plan is
        detached as a task -- see F1's note in the module docstring. Detached
        (rather than awaited here) so running servo work does not stall the
        event loop -- audio for the NEXT response, or a barge-in, must keep
        flowing while a motion is in progress.
        """
        if response.status == "cancelled":
            await self._flush(response.function_calls, _INTERRUPTED_BY_CANCELLED_RESPONSE, reason="cancelled_response")
            return
        is_idle = (response.metadata or {}).get("palmimo_turn") == IDLE_TURN_METADATA["palmimo_turn"]
        epoch = self._epoch
        task = asyncio.ensure_future(self._run_plan(response, epoch, is_idle=is_idle))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_report_failure)

    async def _run_plan(self, response: Response, epoch: int, *, is_idle: bool) -> None:
        """Run a response's function calls in order, honoring the epoch and interrupted-result rules.

        *epoch* is :meth:`handle`'s synchronous snapshot, not a fresh read of
        :attr:`_epoch` -- see the mid-plan abandonment note in the module
        docstring. Idle-tagged
        responses run only their FIRST call (parity with the pipeline
        runtime's own IdleTurn, which never lets one idle tick do more than
        one thing); every other call in that response still gets a "skipped"
        output so its call_id doesn't dangle, and never request a spoken
        continuation.
        """
        calls = response.function_calls
        if not calls:
            return
        if self._epoch != epoch:
            # A barge-in landed between handle()'s snapshot and this task
            # actually getting a turn on the event loop.
            await self._flush(calls, _INTERRUPTED_BY_BARGE_IN, reason="epoch_bumped_before_start")
            return

        run_calls = calls[:1] if is_idle else calls
        if is_idle and len(calls) > 1:
            await self._flush(calls[1:], _SKIPPED_IDLE_TAIL, reason="idle_single_call")

        already_spoke = response.has_message
        saw_image = False
        for index, call in enumerate(run_calls):
            if self._epoch != epoch:
                await self._flush(run_calls[index:], _INTERRUPTED_BY_BARGE_IN, reason="epoch_bumped")
                return
            result_images, interrupted = await self._run_one(call)
            saw_image = saw_image or result_images
            if interrupted:
                await self._flush(run_calls[index + 1 :], _INTERRUPTED_BY_BARGE_IN, reason="interrupted_result")
                return

        if is_idle:
            return
        if self._epoch != epoch:
            # A barge-in landed during the very last call, after every call
            # already resolved cleanly -- the calls are answered, but a NEW
            # response now would compete with whatever the barge-in triggers.
            return
        if saw_image or not already_spoke:
            await self._client.send(ResponseCreate())

    async def _run_one(self, call: FunctionCallItem) -> tuple[bool, bool]:
        """Run one function call; return (saw_image, interrupted)."""
        name = call.name
        try:
            args = json.loads(call.arguments) if call.arguments else {}
        except ValueError:
            args = {}
        print(f"tool > {name}({call.arguments})", flush=True)
        result = await self._view.call(name, args)
        print(f"       -> {result.text[:120]}", flush=True)
        interrupted = result.text.startswith(_INTERRUPTED_PREFIX)
        self._log.write("tool_call", name=name, call_id=call.call_id, text=result.text[:200], is_error=result.is_error)
        # Only a call that ran to completion changes the sleep mode -- see
        # state.py's mirror-and-recovery contract.
        if not result.is_error and not interrupted:
            self._sleeping.observe(name)
        await self._client.send(ItemCreate.function_call_output(call.call_id, result.text))
        # A function result is text by contract, so a photo has to arrive as
        # its own item or the model never sees it.
        for jpeg in result.images:
            await self._client.send(ItemCreate.image(jpeg))
        return bool(result.images), interrupted

    async def _flush(self, remaining: Iterable[FunctionCallItem], message: str, *, reason: str) -> None:
        """Answer every call in *remaining* with *message* so none is left dangling on the model's side."""
        for call in remaining:
            self._log.write("tool_call", name=call.name, call_id=call.call_id, interrupted=True, reason=reason)
            await self._client.send(ItemCreate.function_call_output(call.call_id, message))

    async def settle(self, timeout: float) -> None:
        """Cancel every plan-execution task and wait (bounded) for them to finish.

        Used by :class:`~.app.RealtimeSession`'s shutdown -- ``Palmimo.sleep()``/
        ``wake()`` do not poll a cancel counter, so a tool sitting inside one
        cannot be shortened; waiting forever would hold the process past a
        service manager's stop timeout, which then kills it with the robot
        unparked.
        """
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout)


def _report_failure(task: asyncio.Task[None]) -> None:
    """Surface a plan-run task that died, instead of letting it vanish.

    A failure inside `_run_plan` (a socket send, say) skips the remaining
    calls in that response, so the model waits on call_ids that never
    resolve. With only a discard callback attached, nothing said so.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(f"!! tool run failed: {exc!r}", file=sys.stderr, flush=True)


__all__ = ["ToolBridge"]
