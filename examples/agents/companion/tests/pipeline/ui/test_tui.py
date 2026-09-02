"""Smoke tests for :mod:`palmimo_companion_agent.pipeline.ui.tui` -- the Textual App composes and runs.

Not a full behavioral test of the TUI (that would need to drive the real
:class:`~palmimo_companion_agent.pipeline.wiring.Runtime`, LLM included); this only
checks the app doesn't crash on mount/compose and that typed input reaches
the conductor, using Textual's own :class:`~textual.pilot.Pilot` test driver.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

import palmimo_companion_agent.pipeline.ui.tui as tui_module
from palmimo_companion_agent.core.tools import COMPANION_TOOL_MODELS
from palmimo_companion_agent.pipeline.bus import Bus
from palmimo_companion_agent.pipeline.conductor import Conductor
from palmimo_companion_agent.pipeline.history import History, KeyboardEvent, MicInputEvent
from palmimo_companion_agent.pipeline.settings import PipelineSettings
from palmimo_companion_agent.pipeline.wiring import Runtime
from palmimo_sdk import Palmimo
from palmimo_sdk.agent.toolset import AgentToolSet


class _FakeMessage:
    content: str | None = None
    tool_calls: None = None


class _FakeChoice:
    message = _FakeMessage()


class _FakeResponse:
    choices: ClassVar[list[_FakeChoice]] = [_FakeChoice()]


class FakeLlm:
    """Stands in for :class:`~palmimo_companion_agent.pipeline.llm.LlmProvider`: never calls a real model."""

    async def chat(self, messages: list[dict], *, tools: list[dict] | None = None) -> _FakeResponse:
        return _FakeResponse()


def _toolset(palmimo: Palmimo) -> AgentToolSet:
    ts = AgentToolSet(palmimo, exclude=["creep", "wave"])
    for cls in COMPANION_TOOL_MODELS.values():
        ts.register(cls)
    return ts


def _build_test_runtime() -> Runtime:
    history = History()
    bus = Bus()
    palmimo = Palmimo()
    conductor = Conductor(history, _toolset(palmimo), FakeLlm(), bus, event_log=None)
    return Runtime(conductor=conductor, palmimo=palmimo, toolset=_toolset(palmimo), history=history, event_log=None)


def _settings() -> PipelineSettings:
    return PipelineSettings.with_env_file(None).model_copy(update={"hardware": False})


async def test_app_composes_and_mounts() -> None:
    app = tui_module.CompanionAgentApp(_build_test_runtime())

    async with app.run_test() as pilot:
        assert app.query_one("#log") is not None
        assert app.query_one("#cmd") is not None
        await pilot.pause()

    await app.runtime.aclose()


async def test_typed_input_submits_to_the_conductor() -> None:
    app = tui_module.CompanionAgentApp(_build_test_runtime())

    async with app.run_test() as pilot:
        await pilot.click("#cmd")
        await pilot.press(*"hi there")
        await pilot.press("enter")
        await pilot.pause()

    keyboard_events = [e for e in app.runtime.history.events if isinstance(e, KeyboardEvent)]
    assert any(e.text == "hi there" for e in keyboard_events)

    await app.runtime.aclose()


def test_format_skips_empty_tool_exec_results() -> None:
    from palmimo_companion_agent.pipeline.history import ToolExecEvent

    event = ToolExecEvent(tool_call_id="1", name="stop", arguments="{}", result="interrupted")
    assert tui_module._format(event) == ""


def test_format_renders_a_keyboard_event() -> None:
    event = KeyboardEvent("hello")
    rendered = tui_module._format(event)
    assert "hello" in rendered


def test_format_renders_directed_mic_input_as_heard() -> None:
    event = MicInputEvent("hello there", directed=True)
    rendered = tui_module._format(event)
    assert "heard" in rendered
    assert "overheard" not in rendered


def test_format_renders_ambient_mic_input_as_overheard() -> None:
    event = MicInputEvent("nice weather", directed=False)
    rendered = tui_module._format(event)
    assert "overheard" in rendered


def test_is_exit_command() -> None:
    assert tui_module._is_exit_command("/exit") is True
    assert tui_module._is_exit_command("  /exit  ") is True
    assert tui_module._is_exit_command("/exit now") is False
    assert tui_module._is_exit_command("hello") is False


# ----------------------------------------------------------------------
# run_tui -- connects the robot before Textual's own event loop takes over
# ----------------------------------------------------------------------


def _runtime_over(palmimo: Palmimo) -> Runtime:
    history = History()
    bus = Bus()
    conductor = Conductor(history, _toolset(palmimo), FakeLlm(), bus, event_log=None)
    return Runtime(conductor=conductor, palmimo=palmimo, toolset=_toolset(palmimo), history=history, event_log=None)


class _FakeApp:
    """Stands in for CompanionAgentApp: records the runtime and whether run() was reached."""

    instances: ClassVar[list[_FakeApp]] = []

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.ran = False
        _FakeApp.instances.append(self)

    def run(self) -> None:
        self.ran = True


@pytest.fixture(autouse=True)
def _clear_fake_app_instances() -> None:
    _FakeApp.instances.clear()


def test_run_tui_connects_the_robot_before_entering_the_textual_app(monkeypatch: pytest.MonkeyPatch) -> None:
    palmimo = Palmimo()
    connect_calls: list[int] = []
    monkeypatch.setattr(type(palmimo), "has_connectable_resource", property(lambda self: True))
    monkeypatch.setattr(palmimo, "connect", lambda: connect_calls.append(1))
    monkeypatch.setattr(tui_module, "build_runtime", lambda settings: _runtime_over(palmimo))
    monkeypatch.setattr(tui_module, "CompanionAgentApp", _FakeApp)

    tui_module.run_tui(_settings())

    assert connect_calls == [1]
    [app] = _FakeApp.instances
    assert app.ran is True


def test_run_tui_propagates_a_connect_failure_without_running_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    palmimo = Palmimo()
    monkeypatch.setattr(type(palmimo), "has_connectable_resource", property(lambda self: True))

    def _raise() -> None:
        raise RuntimeError("servo bus not found")

    monkeypatch.setattr(palmimo, "connect", _raise)
    monkeypatch.setattr(tui_module, "build_runtime", lambda settings: _runtime_over(palmimo))
    monkeypatch.setattr(tui_module, "CompanionAgentApp", _FakeApp)

    with pytest.raises(RuntimeError, match="servo bus not found"):
        tui_module.run_tui(_settings())

    assert _FakeApp.instances == []  # the app must never even be constructed on a connect failure
