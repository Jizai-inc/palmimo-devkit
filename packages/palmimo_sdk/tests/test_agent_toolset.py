"""palmimo_sdk.agent.toolset.AgentToolSet — schema export, dispatch, subsetting."""

import asyncio
import threading
from typing import Any, ClassVar, cast

import pytest

from palmimo_sdk import Palmimo
from palmimo_sdk.agent import PalmimoLike
from palmimo_sdk.agent.tools import _STANCE_SETTLE_SECONDS, Tool, ToolResult
from palmimo_sdk.agent.toolset import TOOL_MODELS, AgentToolSet
from palmimo_sdk.robot import MotionCancelled


class FakeRobot:
    """Minimal duck-typed Palmimo double — records calls, never sleeps."""

    def __init__(self, *, run_raises: BaseException | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.display = None
        self.speaker = None
        self.camera = None
        self._run_raises = run_raises
        self.cancel_calls = 0
        self.stop_speech_calls = 0

    def forward(self) -> None:
        self.calls.append(("forward",))

    def wave(self) -> None:
        self.calls.append(("wave",))

    def run(self, *, seconds: float | None = None, steps: int | None = None) -> None:
        self.calls.append(("run", seconds))
        if self._run_raises is not None:
            raise self._run_raises

    def stop(self) -> None:
        self.calls.append(("stop",))

    def look(self, pitch: float = 0.0, yaw: float = 0.0) -> None:
        self.calls.append(("look", pitch, yaw))

    def look_center(self) -> None:
        self.calls.append(("look_center",))

    def set_expression(self, name: str, hold_ms: int = 0) -> None:
        self.calls.append(("set_expression", name, hold_ms))
        return None

    def cancel(self) -> None:
        self.cancel_calls += 1

    def stop_speech(self) -> None:
        self.stop_speech_calls += 1

    def _arm_cancel_scope(self) -> None:
        """No-op: this fake never runs a paced call, so there is nothing to arm."""

    def _disarm_cancel_scope(self) -> None:
        """No-op: pairs with _arm_cancel_scope() above."""


# ----------------------------------------------------------------------
# Schema export
# ----------------------------------------------------------------------


def test_to_openai_tools_covers_every_registered_tool() -> None:
    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()))
    openai_tools = tools.to_openai_tools()
    assert len(openai_tools) == len(TOOL_MODELS)
    names = {t["function"]["name"] for t in openai_tools}
    assert names == set(TOOL_MODELS)


def test_to_openai_tools_shape() -> None:
    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["forward"])
    [entry] = tools.to_openai_tools()
    assert entry["type"] == "function"
    fn = entry["function"]
    assert fn["name"] == "forward"
    assert isinstance(fn["description"], str) and fn["description"]
    assert fn["parameters"]["type"] == "object"
    assert "title" not in fn["parameters"]


# ----------------------------------------------------------------------
# include / exclude subsetting
# ----------------------------------------------------------------------


def test_include_narrows_to_named_tools() -> None:
    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["forward", "stop"])
    assert set(tools.tool_names) == {"forward", "stop"}


def test_exclude_removes_named_tools() -> None:
    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()), exclude=["capture", "say"])
    assert "capture" not in tools.tool_names
    assert "say" not in tools.tool_names
    assert set(tools.tool_names) == set(TOOL_MODELS) - {"capture", "say"}


def test_include_and_exclude_compose() -> None:
    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["forward", "backward", "stop"], exclude=["backward"])
    assert set(tools.tool_names) == {"forward", "stop"}


def test_include_unknown_name_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown tool name"):
        AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["not_a_real_tool"])


def test_exclude_unknown_name_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown tool name"):
        AgentToolSet(cast(PalmimoLike, FakeRobot()), exclude=["not_a_real_tool"])


# ----------------------------------------------------------------------
# register(): extending with a custom Tool
# ----------------------------------------------------------------------


class PingTool(Tool):
    name: ClassVar[str] = "ping"
    description: ClassVar[str] = "Custom test-only tool."

    def execute(self, robot: PalmimoLike) -> ToolResult:
        return ToolResult(text="pong")


async def test_register_adds_a_custom_tool() -> None:
    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["forward"])
    tools.register(PingTool)
    assert "ping" in tools.tool_names
    result = await tools.call("ping", {})
    assert result.text == "pong"


def test_register_appears_in_exported_schemas() -> None:
    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["forward"])
    tools.register(PingTool)
    names = {entry["function"]["name"] for entry in tools.to_openai_tools()}
    assert "ping" in names


# ----------------------------------------------------------------------
# call(): dispatch, dict vs JSON-string args
# ----------------------------------------------------------------------


async def test_call_dispatches_with_dict_args() -> None:
    robot = FakeRobot()
    tools = AgentToolSet(cast(PalmimoLike, robot))
    result = await tools.call("forward", {"seconds": 2.0})
    assert robot.calls == [("forward",), ("run", 2.0), ("stop",), ("run", _STANCE_SETTLE_SECONDS)]
    assert isinstance(result, ToolResult)


async def test_call_accepts_json_string_args() -> None:
    robot = FakeRobot()
    tools = AgentToolSet(cast(PalmimoLike, robot))
    result = await tools.call("forward", '{"seconds": 2.5}')
    assert robot.calls == [("forward",), ("run", 2.5), ("stop",), ("run", _STANCE_SETTLE_SECONDS)]
    assert isinstance(result, ToolResult)


async def test_call_accepts_reason_arg_without_changing_dispatch() -> None:
    robot = FakeRobot()
    tools = AgentToolSet(cast(PalmimoLike, robot))
    result = await tools.call("forward", {"seconds": 2.0, "reason": "greeting"})
    assert robot.calls == [("forward",), ("run", 2.0), ("stop",), ("run", _STANCE_SETTLE_SECONDS)]
    assert isinstance(result, ToolResult)


async def test_call_accepts_empty_string_args_as_no_args() -> None:
    robot = FakeRobot()
    tools = AgentToolSet(cast(PalmimoLike, robot), include=["stop"])
    result = await tools.call("stop", "")
    assert robot.calls == [("stop",), ("look_center",), ("run", 0.6)]
    assert isinstance(result, ToolResult)


async def test_call_accepts_empty_json_object_string() -> None:
    robot = FakeRobot()
    tools = AgentToolSet(cast(PalmimoLike, robot))
    result = await tools.call("forward", "{}")
    assert robot.calls == [
        ("forward",),
        ("run", 1.5),  # default seconds
        ("stop",),
        ("run", _STANCE_SETTLE_SECONDS),
    ]
    assert isinstance(result, ToolResult)


# ----------------------------------------------------------------------
# call(): error paths never raise, always return a ToolResult
# ----------------------------------------------------------------------


async def test_call_unknown_tool_name_lists_available_tools() -> None:
    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["forward", "stop"])
    result = await tools.call("not_a_real_tool", {})
    assert isinstance(result, ToolResult)
    assert "not_a_real_tool" in result.text
    assert "forward" in result.text
    assert "stop" in result.text
    assert result.is_error is True


async def test_call_invalid_arguments_reports_validation_error_text() -> None:
    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()))
    result = await tools.call("forward", {"seconds": 999.0})
    assert isinstance(result, ToolResult)
    assert "forward" in result.text
    assert "Invalid arguments" in result.text
    assert result.is_error is True


async def test_call_missing_required_argument_reports_validation_error_text() -> None:
    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()))
    result = await tools.call("turn", {"seconds": 1.0})  # missing required `direction`
    assert isinstance(result, ToolResult)
    assert "Invalid arguments" in result.text
    assert result.is_error is True


async def test_call_malformed_json_string_reports_error_text() -> None:
    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()))
    result = await tools.call("forward", "{not valid json")
    assert isinstance(result, ToolResult)
    assert "Invalid JSON" in result.text
    assert result.is_error is True


async def test_call_execute_time_exception_is_caught_and_reported() -> None:
    class BoomTool(Tool):
        name: ClassVar[str] = "boom"
        description: ClassVar[str] = "Always raises, for testing call()'s error handling."

        def execute(self, robot: PalmimoLike) -> ToolResult:
            raise RuntimeError("kaboom")

    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["forward"])
    tools.register(BoomTool)
    result = await tools.call("boom", {})
    assert isinstance(result, ToolResult)
    assert "boom" in result.text
    assert "kaboom" in result.text
    assert result.is_error is True


async def test_call_successful_execution_leaves_is_error_false() -> None:
    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()))
    result = await tools.call("forward", {"seconds": 1.0})
    assert result.is_error is False


async def test_call_reports_run_exception_but_still_stops_the_robot() -> None:
    """A timed motion tool's run() raising must surface as an observation
    string (via call()'s catch-all) *and* still stop the robot -- see the
    stop-guarantee added for exactly this case."""
    boom = RuntimeError("driver write failed")
    robot = FakeRobot(run_raises=boom)
    tools = AgentToolSet(cast(PalmimoLike, robot), include=["forward"])
    result = await tools.call("forward", {"seconds": 1.0})
    assert isinstance(result, ToolResult)
    assert "forward" in result.text
    assert "driver write failed" in result.text
    assert robot.calls == [("forward",), ("run", 1.0), ("stop",)]


# ----------------------------------------------------------------------
# call(): MotionCancelled is special-cased -- an interruption, not an error.
# ----------------------------------------------------------------------


async def test_call_motion_cancelled_is_reported_as_interrupted_not_error() -> None:
    class CancelledTool(Tool):
        name: ClassVar[str] = "cancelled_tool"
        description: ClassVar[str] = "Always raises MotionCancelled, for testing call()'s special-casing."

        def execute(self, robot: PalmimoLike) -> ToolResult:
            raise MotionCancelled("Palmimo.cancel() was called; motion aborted mid-run().")

    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["forward"])
    tools.register(CancelledTool)
    result = await tools.call("cancelled_tool", {})
    assert isinstance(result, ToolResult)
    assert result.is_error is False
    assert result.text.startswith("interrupted:")


async def test_call_motion_cancelled_end_to_end_against_real_palmimo() -> None:
    """A real Palmimo.cancel()/MotionCancelled, not a fake, still comes back
    as a non-error 'interrupted' ToolResult -- see robot.py's cancel()
    docstring for the on_step-triggered self-cancel pattern this borrows."""

    def on_step(_pos: dict[str, int]) -> None:
        robot.cancel()

    robot = Palmimo(fps=200, on_step=on_step)  # cancels itself on the first frame
    tools = AgentToolSet(robot, include=["forward"])
    result = await tools.call("forward", {"seconds": 5.0})
    assert result.is_error is False
    assert result.text.startswith("interrupted:")
    assert robot.motion == "idle"  # _run_motion's finally still ran stop()


# ----------------------------------------------------------------------
# is_busy() / cancel_running()
# ----------------------------------------------------------------------


async def test_is_busy_false_when_idle() -> None:
    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()))
    assert tools.is_busy() is False


async def test_is_busy_true_while_call_in_flight() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingTool(Tool):
        name: ClassVar[str] = "blocking"
        description: ClassVar[str] = "Blocks until released, for testing is_busy()."

        def execute(self, robot: PalmimoLike) -> ToolResult:
            entered.set()
            release.wait(timeout=2.0)
            return ToolResult(text="ran")

    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    tools.register(BlockingTool)

    call_task = asyncio.ensure_future(tools.call("blocking", {}))
    await asyncio.get_event_loop().run_in_executor(None, entered.wait, 2.0)
    assert tools.is_busy() is True

    release.set()
    result = await call_task
    assert result.text == "ran"
    assert tools.is_busy() is False


async def test_cancel_running_is_a_noop_for_motion_when_idle() -> None:
    """robot.cancel() (motion) is only called while busy -- there is no
    in-flight tool call to abort when idle."""
    robot = FakeRobot()
    tools = AgentToolSet(cast(PalmimoLike, robot))
    await tools.cancel_running()
    assert robot.cancel_calls == 0


async def test_cancel_running_stops_speech_even_when_idle() -> None:
    """robot.stop_speech() is called unconditionally: SayTool's background
    speech often outlives the tool call itself (see SayTool's brief join),
    so there is frequently no in-flight call -- is_busy() False -- while
    speech is still playing. A barge-in must interrupt that speech too."""
    robot = FakeRobot()
    tools = AgentToolSet(cast(PalmimoLike, robot))
    assert tools.is_busy() is False
    await tools.cancel_running()
    assert robot.stop_speech_calls == 1


async def test_cancel_running_calls_robot_cancel_while_busy() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingTool(Tool):
        name: ClassVar[str] = "blocking"
        description: ClassVar[str] = "Blocks until released, for testing cancel_running()."

        def execute(self, robot: PalmimoLike) -> ToolResult:
            entered.set()
            release.wait(timeout=2.0)
            return ToolResult(text="ran")

    robot = FakeRobot()
    tools = AgentToolSet(cast(PalmimoLike, robot), include=["stop"])
    tools.register(BlockingTool)

    call_task = asyncio.ensure_future(tools.call("blocking", {}))
    await asyncio.get_event_loop().run_in_executor(None, entered.wait, 2.0)

    await tools.cancel_running()
    assert robot.cancel_calls == 1
    assert robot.stop_speech_calls == 1  # both sides of a barge-in fire together

    release.set()
    await call_task


async def test_call_returns_interrupted_without_executing_when_cancelled_before_dispatch() -> None:
    """Exercises the stale-cancel window documented on call(): a
    cancel_running() that lands in the deliberate checkpoint just before
    execute() is handed to a worker thread must skip execute() entirely."""
    executed: list[str] = []

    class RecordingTool(Tool):
        name: ClassVar[str] = "recording"
        description: ClassVar[str] = "Records whether it ran, for testing the pre-dispatch stale-cancel check."

        def execute(self, robot: PalmimoLike) -> ToolResult:
            executed.append("ran")
            return ToolResult(text="ran")

    tools = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    tools.register(RecordingTool)

    # asyncio.gather() schedules its coroutines as tasks in argument order,
    # so call()'s task runs first -- reaching (and suspending at) its own
    # `await asyncio.sleep(0)` checkpoint before this task's very first line
    # runs. That ordering is what lets a same-turn cancel_running() land
    # inside call()'s checkpoint instead of racing it.
    async def do_cancel() -> None:
        await tools.cancel_running()

    result, _ = await asyncio.gather(tools.call("recording", {}), do_cancel())

    assert executed == []
    assert result.is_error is False
    assert result.text.startswith("interrupted:")


# ----------------------------------------------------------------------
# call(): the calling task's own CancelledError is not swallowed.
# ----------------------------------------------------------------------


async def test_call_propagates_cancelled_error_and_stops_the_robot_first() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingTool(Tool):
        name: ClassVar[str] = "blocking"
        description: ClassVar[str] = "Blocks until released, for testing outer-task CancelledError handling."

        def execute(self, robot: PalmimoLike) -> ToolResult:
            entered.set()
            release.wait(timeout=2.0)
            robot.stop()
            return ToolResult(text="ran")

    robot = FakeRobot()
    tools = AgentToolSet(cast(PalmimoLike, robot), include=["stop"])
    tools.register(BlockingTool)

    call_task = asyncio.ensure_future(tools.call("blocking", {}))
    await asyncio.get_event_loop().run_in_executor(None, entered.wait, 2.0)

    call_task.cancel()
    release.set()  # let the worker thread actually finish so the awaited task can settle
    with pytest.raises(asyncio.CancelledError):
        await call_task

    assert robot.cancel_calls == 1
    assert ("stop",) in robot.calls
    assert tools.is_busy() is False


async def test_call_second_cancellation_while_awaiting_shielded_worker_does_not_abandon_it() -> None:
    """A SECOND cancellation of the calling task, delivered while call() is
    already looping on the shielded worker after a first cancellation, must
    not make call() return/raise before the worker thread actually
    finishes -- both CancelledErrors are absorbed by the shield-and-retry
    loop until task.done()."""
    entered = threading.Event()
    finished = threading.Event()
    release = threading.Event()

    class SlowTool(Tool):
        name: ClassVar[str] = "slow"
        description: ClassVar[str] = "Blocks until released, for testing double-cancellation."

        def execute(self, robot: PalmimoLike) -> ToolResult:
            entered.set()
            release.wait(timeout=2.0)
            finished.set()
            return ToolResult(text="ran")

    robot = FakeRobot()
    tools = AgentToolSet(cast(PalmimoLike, robot), include=["stop"])
    tools.register(SlowTool)

    call_task = asyncio.ensure_future(tools.call("slow", {}))
    await asyncio.get_event_loop().run_in_executor(None, entered.wait, 2.0)

    call_task.cancel()
    await asyncio.sleep(0)  # let call_task's except-CancelledError block start its wait loop
    call_task.cancel()  # a second cancellation while already waiting on the worker
    await asyncio.sleep(0)

    assert not finished.is_set()  # the worker is still blocked -- neither cancel finished it early
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await call_task

    assert finished.is_set()  # the worker ran to completion before call_task unwound
    assert robot.cancel_calls == 1  # robot.cancel() is issued once, on the first CancelledError


# ----------------------------------------------------------------------
# call(): _arm_cancel_scope() / _disarm_cancel_scope() around dispatch --
# closing the dispatch-to-run()-entry cancel window.
# ----------------------------------------------------------------------


async def test_call_arm_disarm_closes_dispatch_to_run_entry_window() -> None:
    """A cancel() delivered after call() has armed the cancel scope and
    dispatched the worker thread, but BEFORE the worker's tool.execute() has
    actually reached robot.run(), must still raise MotionCancelled once
    run() starts pacing -- the structural fix for the window _arm_cancel_scope()
    / _disarm_cancel_scope() close. Deterministic: an Event stalls the worker
    thread right after entry and before it calls run(), so the cancel() is
    guaranteed to land in exactly that window."""
    reached_execute = threading.Event()
    proceed = threading.Event()

    class DelayedRunTool(Tool):
        name: ClassVar[str] = "delayed_run"
        description: ClassVar[str] = "Blocks before calling robot.run(), for testing the arm/dispatch race."

        def execute(self, robot: PalmimoLike) -> ToolResult:
            reached_execute.set()
            proceed.wait(timeout=2.0)
            robot.forward()
            try:
                robot.run(steps=100)
            finally:
                # Mirrors the real _run_motion contract (see tools.py): a
                # cancelled/failed motion always lands back on stop().
                robot.stop()
            return ToolResult(text="ran")

    robot = Palmimo(fps=1000)  # real facade: FakeRobot doesn't implement run()'s pacing/cancel contract
    tools = AgentToolSet(robot, include=["stop"])
    tools.register(DelayedRunTool)

    call_task = asyncio.ensure_future(tools.call("delayed_run", {}))
    await asyncio.get_event_loop().run_in_executor(None, reached_execute.wait, 2.0)

    # The worker thread has been dispatched (call() has already armed the
    # cancel scope) but is still blocked before its own robot.run() call.
    robot.cancel()
    proceed.set()

    result = await call_task
    assert result.is_error is False
    assert result.text.startswith("interrupted:")
    assert robot.motion == "idle"


async def test_call_disarms_after_a_tool_that_never_paces_so_it_does_not_leak() -> None:
    """A tool whose execute() never reaches a paced call (run() / perform_dance()
    / play_realtime()) leaves the armed scope unconsumed; call()'s `finally`
    must still disarm it, so it cannot bleed into a LATER, unrelated call's
    cancel semantics."""

    class NoOpTool(Tool):
        name: ClassVar[str] = "noop"
        description: ClassVar[str] = "Does nothing (never paces), for testing arm/disarm leak."

        def execute(self, robot: PalmimoLike) -> ToolResult:
            return ToolResult(text="noop")

    robot = Palmimo(fps=1000)
    tools = AgentToolSet(robot, include=["stop"])
    tools.register(NoOpTool)

    result = await tools.call("noop", {})
    assert result.text == "noop"
    assert robot._armed_snapshot is None  # disarmed after the call -- nothing left armed

    # A cancel() delivered well before the NEXT run() must not bleed into it --
    # the pre-existing "cancel while idle" contract, undisturbed by the
    # earlier no-op call's arm/disarm pair.
    robot.cancel()
    robot.forward()
    pos = robot.run(steps=5)
    assert len(pos) == 20


async def test_call_direct_run_without_toolset_keeps_unarmed_semantics() -> None:
    """A caller that drives Palmimo.run() directly -- never through
    AgentToolSet.call(), so never arming a scope -- keeps exactly the
    original, unarmed cancel semantics: an idle cancel() does not bleed into
    the next run()."""
    robot = Palmimo(fps=1000)
    assert robot._armed_snapshot is None
    robot.cancel()  # idle cancel, no toolset involved, no scope armed
    robot.forward()
    pos = robot.run(steps=5)  # must NOT raise
    assert len(pos) == 20


# ----------------------------------------------------------------------
# Integration smoke: real (compute-only) Palmimo, non-sleeping tools only.
# ----------------------------------------------------------------------


async def test_call_look_and_stop_against_real_compute_only_palmimo() -> None:
    robot = Palmimo()
    tools = AgentToolSet(robot, include=["look", "stop"])
    look_result = await tools.call("look", {"pitch": 10.0, "yaw": -10.0})
    stop_result = await tools.call("stop", {})
    assert isinstance(look_result, ToolResult)
    assert isinstance(stop_result, ToolResult)
    assert robot.motion == "idle"
