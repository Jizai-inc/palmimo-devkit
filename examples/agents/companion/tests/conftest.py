"""Shared test scaffolding for tests/core/ -- fakes for the LLM/toolset boundary.

Exposed as pytest fixtures (rather than plain module-level names test files
import) because this project's pytest config uses ``--import-mode=importlib``
with no ``__init__.py`` anywhere under ``tests/`` -- a sibling test module
cannot reliably do ``from conftest import ...`` under that mode, while
fixture injection works regardless of import mode.

None of this hits a network or real hardware: :func:`toolset` builds a
compute-only :class:`~palmimo_sdk.Palmimo`, and :class:`FakeLlmProvider`
replays a scripted list of ``chat()`` outcomes instead of calling a model.
Shared here (rather than duplicated per test module) since
:mod:`~palmimo_companion_agent.pipeline.idle`, :mod:`~palmimo_companion_agent.pipeline.respond`,
and :mod:`~palmimo_companion_agent.pipeline.conductor` all drive the exact same
LiteLLM-shaped ``ModelResponse`` surface.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from palmimo_companion_agent.core.tools import COMPANION_TOOL_MODELS
from palmimo_companion_agent.pipeline.history import History
from palmimo_companion_agent.pipeline.llm import SpeechVerdict
from palmimo_sdk import Palmimo
from palmimo_sdk.agent.toolset import AgentToolSet


@pytest.fixture
def toolset() -> AgentToolSet:
    """A full companion :class:`~palmimo_sdk.agent.toolset.AgentToolSet` over a fresh compute-only Palmimo."""
    ts = AgentToolSet(Palmimo(), exclude=["creep", "wave"])
    for cls in COMPANION_TOOL_MODELS.values():
        ts.register(cls)
    return ts


@pytest.fixture
def events_of_type() -> Callable[[History, type], list[Any]]:
    """Factory: ``events_of_type(history, cls)`` -> every *history* event that is an instance of *cls*."""

    def _events_of_type(history: History, cls: type) -> list[Any]:
        return [event for event in history.events if isinstance(event, cls)]

    return _events_of_type


class _FakeFunction:
    def __init__(self, name: str | None, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: dict) -> None:
        self.id = call_id
        self.function = _FakeFunction(name, json.dumps(arguments))


class _FakeMessage:
    def __init__(self, *, content: str | None = None, tool_calls: list[_FakeToolCall] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message: _FakeMessage) -> None:
        self.message = message


class _FakeResponse:
    def __init__(self, choices: list[_FakeChoice]) -> None:
        self.choices = choices


def _tool_call_turn(
    name: str, arguments: dict, *, call_id: str = "call-1", content: str | None = None
) -> _FakeResponse:
    return _FakeResponse(
        [_FakeChoice(_FakeMessage(content=content, tool_calls=[_FakeToolCall(call_id, name, arguments)]))]
    )


def _plan_turn(calls: list[tuple[str, dict]], *, content: str | None = None) -> _FakeResponse:
    tool_calls = [_FakeToolCall(f"call-{i}", name, args) for i, (name, args) in enumerate(calls)]
    return _FakeResponse([_FakeChoice(_FakeMessage(content=content, tool_calls=tool_calls))])


def _plan_turn_with_raw_arguments(
    entries: list[tuple[str, dict | str]], *, content: str | None = None
) -> _FakeResponse:
    """Like ``plan_turn``, but an entry's arguments may be a raw (possibly malformed) JSON string instead of a dict."""
    tool_calls: list[_FakeToolCall] = []
    for i, (name, args) in enumerate(entries):
        if isinstance(args, str):
            call = _FakeToolCall(f"call-{i}", name, {})
            call.function.arguments = args
        else:
            call = _FakeToolCall(f"call-{i}", name, args)
        tool_calls.append(call)
    return _FakeResponse([_FakeChoice(_FakeMessage(content=content, tool_calls=tool_calls))])


def _raw_tool_call_turn(name: str, raw_arguments: str, *, call_id: str = "call-1") -> _FakeResponse:
    call = _FakeToolCall(call_id, name, {})
    call.function.arguments = raw_arguments
    return _FakeResponse([_FakeChoice(_FakeMessage(tool_calls=[call]))])


def _text_turn(content: str | None) -> _FakeResponse:
    return _FakeResponse([_FakeChoice(_FakeMessage(content=content))])


def _empty_choices_turn() -> _FakeResponse:
    return _FakeResponse([])


@pytest.fixture
def tool_call_turn() -> Callable[..., _FakeResponse]:
    """Factory: a ``chat()`` outcome carrying exactly one tool call."""
    return _tool_call_turn


@pytest.fixture
def plan_turn() -> Callable[..., _FakeResponse]:
    """Factory: a ``chat()`` outcome carrying a multi-call plan (respond's own shape)."""
    return _plan_turn


@pytest.fixture
def plan_turn_with_raw_arguments() -> Callable[..., _FakeResponse]:
    """Factory: like ``plan_turn``, but an entry may carry a raw (possibly malformed) JSON argument string."""
    return _plan_turn_with_raw_arguments


@pytest.fixture
def raw_tool_call_turn() -> Callable[..., _FakeResponse]:
    """Factory: a ``chat()`` outcome carrying one tool call with a raw (possibly malformed) JSON argument string."""
    return _raw_tool_call_turn


@pytest.fixture
def text_turn() -> Callable[[str | None], _FakeResponse]:
    """Factory: a ``chat()`` outcome with no tool call -- just (possibly None) assistant text."""
    return _text_turn


@pytest.fixture
def empty_choices_turn() -> Callable[[], _FakeResponse]:
    """Factory: a ``chat()`` outcome with an empty ``choices`` list (a malformed/degenerate response)."""
    return _empty_choices_turn


class FakeLlmProvider:
    """Replays a fixed script of ``chat()`` outcomes; falls back to a tool-free (idle) turn once exhausted.

    Also stands in for the VLM (:meth:`describe_image`) and the speech guard
    (:meth:`classify_speech`, via :attr:`classify_result`) -- the full
    :class:`~palmimo_companion_agent.pipeline.llm.LlmProvider` surface the
    conductor and both turn types call.
    """

    def __init__(self, turns: list[Any] | None = None) -> None:
        self._turns = list(turns) if turns is not None else []
        self.calls: list[dict[str, Any]] = []
        self.described: list[str] = []
        self.classify_calls: list[str] = []
        #: Overridden per test to script :meth:`classify_speech`'s return
        #: (or, if an ``Exception`` instance, what it raises).
        self.classify_result: SpeechVerdict | Exception = SpeechVerdict(kind="command", text=None)

    async def chat(
        self, messages: list[dict], *, tools: list[dict] | None = None, parallel_tool_calls: bool = False, **_: Any
    ) -> _FakeResponse:
        self.calls.append({"messages": messages, "tools": tools, "parallel_tool_calls": parallel_tool_calls})
        if not self._turns:
            return _text_turn(None)
        outcome = self._turns.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def describe_image(self, image_b64: str, prompt: str, *, max_tokens: int = 200) -> str:
        self.described.append(image_b64)
        return "a fake scene"

    async def classify_speech(self, text: str) -> SpeechVerdict:
        self.classify_calls.append(text)
        if isinstance(self.classify_result, Exception):
            raise self.classify_result
        return self.classify_result


@pytest.fixture
def fake_llm_provider() -> type[FakeLlmProvider]:
    """The :class:`FakeLlmProvider` class itself, so a test can construct it with its own scripted turns."""
    return FakeLlmProvider
