"""Tests for :mod:`palmimo_companion_agent.core.reflexes`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

from palmimo_companion_agent.core.perception import Detection, DetectionKind
from palmimo_companion_agent.core.reflexes import ReflexEngine
from palmimo_companion_agent.core.tools import LOOK_AT_FACE_SEEN_MARK


class FakeToolset:
    """Records calls; ``busy`` / ``look_at_face_result`` are test-controlled."""

    def __init__(self, *, busy: bool = False, look_at_face_result: str = LOOK_AT_FACE_SEEN_MARK) -> None:
        self.busy = busy
        self.look_at_face_result = look_at_face_result
        self.calls: list[tuple[str, dict]] = []

    def is_busy(self) -> bool:
        return self.busy

    async def call(self, name: str, args: dict) -> Any:
        from palmimo_sdk.agent.tools import ToolResult

        self.calls.append((name, args))
        if name == "wave_both":
            return ToolResult(text="waved both front legs")
        if name == "look_at_face":
            return ToolResult(text=self.look_at_face_result)
        if name == "show_emoji":
            return ToolResult(text="face shown")
        raise AssertionError(f"unexpected tool call: {name}")


def _detections(items: list[Detection]) -> AsyncIterator[Detection]:
    async def _gen() -> AsyncIterator[Detection]:
        for item in items:
            yield item

    return _gen()


def _recorder() -> tuple[list[str], Callable[[str], None]]:
    """A ``notify`` callback plus the list it appends to -- stands in for History without depending on it."""
    notes: list[str] = []
    return notes, notes.append


async def test_wave_detection_waves_back_with_a_happy_face() -> None:
    toolset = FakeToolset()
    _, notify = _recorder()
    engine = ReflexEngine(toolset, notify)

    await engine.run(_detections([Detection(summary="someone is waving", kind=DetectionKind.WAVE)]))

    [name_args] = [(n, a) for n, a in toolset.calls if n == "wave_both"]
    assert name_args[1]["face"] == "HAPPY"


async def test_wave_detection_notifies_the_caller() -> None:
    toolset = FakeToolset()
    notes, notify = _recorder()
    engine = ReflexEngine(toolset, notify)

    await engine.run(_detections([Detection(summary="someone is waving", kind=DetectionKind.WAVE)]))

    assert len(notes) == 1
    assert "waved back" in notes[0]


async def test_face_detection_looks_at_the_face() -> None:
    toolset = FakeToolset()
    _, notify = _recorder()
    engine = ReflexEngine(toolset, notify)

    await engine.run(_detections([Detection(summary="a face", kind=DetectionKind.FACE)]))

    assert any(name == "look_at_face" for name, _ in toolset.calls)


async def test_face_detection_notifies_the_caller_only_when_the_face_was_actually_seen() -> None:
    toolset = FakeToolset(look_at_face_result=LOOK_AT_FACE_SEEN_MARK)
    notes, notify = _recorder()
    engine = ReflexEngine(toolset, notify)

    await engine.run(_detections([Detection(summary="a face", kind=DetectionKind.FACE)]))

    assert len(notes) == 1
    assert "looked at a face" in notes[0]


async def test_face_detection_notifies_nothing_when_the_face_was_not_seen() -> None:
    toolset = FakeToolset(look_at_face_result="tried to look at a face but none was found")
    notes, notify = _recorder()
    engine = ReflexEngine(toolset, notify)

    await engine.run(_detections([Detection(summary="a face", kind=DetectionKind.FACE)]))

    assert notes == []


async def test_a_busy_toolset_skips_the_reflex_entirely() -> None:
    toolset = FakeToolset(busy=True)
    notes, notify = _recorder()
    engine = ReflexEngine(toolset, notify)

    await engine.run(_detections([Detection(summary="someone is waving", kind=DetectionKind.WAVE)]))

    assert toolset.calls == []
    assert notes == []


async def test_an_inhibited_engine_skips_the_reflex_entirely() -> None:
    """A sleeping robot must not be waved or tracked -- see the module docstring."""
    toolset = FakeToolset()
    _, notify = _recorder()
    engine = ReflexEngine(toolset, notify, inhibited=lambda: True)

    await engine.run(_detections([Detection(summary="someone is waving", kind=DetectionKind.WAVE)]))

    assert toolset.calls == []


async def test_inhibited_defaults_to_never_so_existing_callers_are_unaffected() -> None:
    toolset = FakeToolset()
    _, notify = _recorder()
    engine = ReflexEngine(toolset, notify)  # no inhibited= given

    await engine.run(_detections([Detection(summary="someone is waving", kind=DetectionKind.WAVE)]))

    assert any(name == "wave_both" for name, _ in toolset.calls)


async def test_an_unconfigured_kind_is_ignored() -> None:
    toolset = FakeToolset()
    _, notify = _recorder()
    engine = ReflexEngine(toolset, notify)

    # A kind outside the current vocabulary (forward-compat / defensive
    # runtime check, not something DetectionKind can express today).
    unconfigured_kind = cast(DetectionKind, "mystery")
    await engine.run(_detections([Detection(summary="something else", kind=unconfigured_kind)]))

    assert toolset.calls == []


async def test_a_second_wave_within_the_cooldown_window_is_skipped() -> None:
    toolset = FakeToolset()
    _, notify = _recorder()
    engine = ReflexEngine(toolset, notify)

    await engine.run(
        _detections(
            [
                Detection(summary="wave 1", kind=DetectionKind.WAVE),
                Detection(summary="wave 2", kind=DetectionKind.WAVE),
            ]
        )
    )

    assert [n for n, _ in toolset.calls].count("wave_both") == 1


async def test_a_reflex_that_raises_is_swallowed() -> None:
    class RaisingToolset(FakeToolset):
        async def call(self, name: str, args: dict) -> Any:
            if name == "wave_both":
                raise RuntimeError("servo fault")
            return await super().call(name, args)

    toolset = RaisingToolset()
    _, notify = _recorder()
    engine = ReflexEngine(toolset, notify)

    # Must not raise out of run().
    await engine.run(_detections([Detection(summary="someone is waving", kind=DetectionKind.WAVE)]))


async def test_busy_skip_does_not_consume_the_cooldown() -> None:
    """A busy-skip must not block a later, un-busy retry within the same cooldown window."""
    toolset = FakeToolset(busy=True)
    _, notify = _recorder()
    engine = ReflexEngine(toolset, notify)

    await engine._handle(Detection(summary="wave", kind=DetectionKind.WAVE))
    assert toolset.calls == []

    toolset.busy = False
    await engine._handle(Detection(summary="wave", kind=DetectionKind.WAVE))

    assert any(n == "wave_both" for n, _ in toolset.calls)


async def test_watch_style_consumption_stops_when_the_iterator_ends() -> None:
    toolset = FakeToolset()
    _, notify = _recorder()
    engine = ReflexEngine(toolset, notify)

    # An empty async generator should just return immediately.
    await asyncio.wait_for(engine.run(_detections([])), timeout=1.0)


async def test_a_name_call_shows_a_face_before_it_looks_for_anyone() -> None:
    """The expression is the only part that works without a camera, so it goes first."""
    toolset = FakeToolset()
    notes, notify = _recorder()
    engine = ReflexEngine(toolset, notify)

    await engine.run(_detections([Detection(summary="someone called the robot by name", kind=DetectionKind.NAME_CALL)]))

    assert [name for name, _ in toolset.calls] == ["show_emoji", "look_at_face"]
    assert toolset.calls[0][1]["emoji"] == "HAPPY"
    assert any("answered a call" in note for note in notes)


async def test_a_name_call_is_answered_even_when_no_face_is_found() -> None:
    """Someone calling from behind the robot still gets an acknowledgement."""
    toolset = FakeToolset(look_at_face_result="no camera attached; could not look for a face")
    notes, notify = _recorder()
    engine = ReflexEngine(toolset, notify)

    await engine.run(_detections([Detection(summary="someone called the robot by name", kind=DetectionKind.NAME_CALL)]))

    assert any("answered a call" in note for note in notes)
    assert not any("found the caller" in note for note in notes)


async def test_a_name_call_is_skipped_while_the_robot_is_busy() -> None:
    toolset = FakeToolset(busy=True)
    _, notify = _recorder()
    engine = ReflexEngine(toolset, notify)

    await engine.run(_detections([Detection(summary="called", kind=DetectionKind.NAME_CALL)]))

    assert toolset.calls == []


async def test_calling_twice_in_quick_succession_answers_once() -> None:
    """A discrete act, so the cooldown is short -- but not zero."""
    toolset = FakeToolset()
    _, notify = _recorder()
    engine = ReflexEngine(toolset, notify)

    calls = [Detection(summary="called", kind=DetectionKind.NAME_CALL)] * 2
    await engine.run(_detections(calls))

    assert [name for name, _ in toolset.calls].count("show_emoji") == 1
