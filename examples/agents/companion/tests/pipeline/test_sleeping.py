"""Tests for :mod:`palmimo_companion_agent.pipeline.sleeping`."""

from __future__ import annotations

from palmimo_companion_agent.pipeline.history import CameraEvent, ToolExecEvent
from palmimo_companion_agent.pipeline.sleeping import Sleeping


def _exec(name: str, result: str, *, error: bool = False) -> ToolExecEvent:
    return ToolExecEvent(tool_call_id="t1", name=name, arguments="{}", result=result, error=error)


def test_starts_awake() -> None:
    assert Sleeping().asleep is False


def test_a_successful_sleep_flips_to_asleep() -> None:
    sleeping = Sleeping()
    sleeping.observe(_exec("sleep", "went to sleep"))
    assert sleeping.asleep is True


def test_a_successful_wake_up_flips_back_to_awake() -> None:
    sleeping = Sleeping()
    sleeping.observe(_exec("sleep", "went to sleep"))
    sleeping.observe(_exec("wake_up", "woke up and stretched"))
    assert sleeping.asleep is False


def test_an_interrupted_sleep_does_not_flip_the_mirror() -> None:
    sleeping = Sleeping()
    sleeping.observe(_exec("sleep", "interrupted: ..."))
    assert sleeping.asleep is False


def test_an_interrupted_wake_up_does_not_flip_the_mirror() -> None:
    sleeping = Sleeping()
    sleeping.observe(_exec("sleep", "went to sleep"))
    sleeping.observe(_exec("wake_up", "interrupted: ..."))
    assert sleeping.asleep is True  # stays asleep -- the wake_up never actually finished


def test_a_locally_failed_sleep_does_not_flip_the_mirror() -> None:
    """A dispatch-side failure (malformed args, a raised exception) -- see
    execute_and_record's own "execution error: ..." spelling, tagged error=True."""
    sleeping = Sleeping()
    sleeping.observe(_exec("sleep", "execution error: servo fault", error=True))
    assert sleeping.asleep is False


def test_a_locally_failed_wake_up_does_not_flip_the_mirror() -> None:
    sleeping = Sleeping()
    sleeping.observe(_exec("sleep", "went to sleep"))
    sleeping.observe(_exec("wake_up", "execution error: servo fault", error=True))
    assert sleeping.asleep is True


def test_a_tool_level_is_error_sleep_does_not_flip_the_mirror() -> None:
    """A ToolResult.is_error the tool itself set (not a locally-spelled
    "execution error:" string) -- e.g. AgentToolSet's own "Tool 'sleep'
    raised an error while executing: ..." wrapping. Only the error flag
    matters, not how the text happens to read."""
    sleeping = Sleeping()
    sleeping.observe(_exec("sleep", "Tool 'sleep' raised an error while executing: servo fault", error=True))
    assert sleeping.asleep is False


def test_a_tool_level_is_error_wake_up_does_not_flip_the_mirror() -> None:
    sleeping = Sleeping()
    sleeping.observe(_exec("sleep", "went to sleep"))
    sleeping.observe(_exec("wake_up", "Tool 'wake_up' raised an error while executing: servo fault", error=True))
    assert sleeping.asleep is True


def test_unrelated_tool_calls_are_ignored() -> None:
    sleeping = Sleeping()
    sleeping.observe(_exec("rest", "rested for a moment"))
    assert sleeping.asleep is False


def test_non_tool_exec_events_are_ignored() -> None:
    sleeping = Sleeping()
    sleeping.observe(CameraEvent("a fake scene"))
    assert sleeping.asleep is False
