"""ToolView -- a read-only, per-turn-kind view over the shared :class:`~palmimo_sdk.agent.toolset.AgentToolSet`.

Idle and respond turns see different slices of the same toolset: idle is
restricted to a small, speech-free vocabulary (:data:`IDLE_TOOL_NAMES`),
respond sees everything. Never mutates the underlying toolset's tool
classes or schema dicts -- both views share one
:class:`~palmimo_sdk.agent.toolset.AgentToolSet` instance (and its
cancellation/busy state).
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from palmimo_sdk.agent.tools import ToolResult


if TYPE_CHECKING:
    from palmimo_sdk.agent.toolset import AgentToolSet


#: Idle turn's restricted tool vocabulary -- motion/expression tools only, no
#: speech and no showy/social gestures (those are reserved for a response to
#: an actual person). See core/prompts/idle.md for the behavior this backs.
IDLE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "look",
        "look_center",
        "nod",
        "head_shake",
        "body_tilt",
        "stretch",
        "turn",
        "rest",
        "set_face",
        "show_emoji",
        "capture",
        "discover",
        "investigate",
        "eye_contact",
        "look_at_face",
    }
)

#: Every tool reserved for the respond turn -- speech (``say``), showy/social
#: gestures, and locomotion. Named explicitly (not derived as "everything
#: not idle") so a test can assert the two sets exactly partition the
#: registered tools -- see test_toolview.py's partition test, which fails
#: loudly if a new SDK/agent tool goes unclassified.
RESPOND_ONLY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "backward",
        "bow",
        "clap",
        "dance",
        "forward",
        "pushup",
        "say",
        "sleep",
        "stop",
        "strafe",
        "wake_up",
        "wave_both",
    }
)


class ToolView:
    """A filtered, squash-say-capable view over one shared :class:`AgentToolSet`.

    Args:
        allow: If given, only these tool names are visible/callable through
            this view. ``None`` (default) exposes every registered tool.
        squash_say: If True, a ``say`` key in a call's arguments is dropped
            before dispatch -- used by the idle view so a hallucinated
            ``say`` argument on an otherwise-idle tool becomes a voiceless
            gesture instead of an accidental idle-loop utterance.
    """

    def __init__(self, toolset: AgentToolSet, *, allow: frozenset[str] | None = None, squash_say: bool = False) -> None:
        self._toolset = toolset
        self._allow = allow
        self._squash_say = squash_say

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """This view's tools as OpenAI ``tools=`` entries, filtered by the allowlist.

        When :attr:`_squash_say` is set, each tool's ``say`` property (and
        any ``required`` entry naming it) is also removed from the returned
        schema -- not just squashed at dispatch time -- so a model driven
        from this view is never even offered the argument (idle.md's claim
        that ``say`` isn't in its schema at all must stay true). Deep-copied
        so this never mutates the underlying toolset's own schema dicts,
        which the respond view (and any other view) also reads.
        """
        tools = self._toolset.to_openai_tools()
        if self._allow is not None:
            tools = [tool for tool in tools if tool["function"]["name"] in self._allow]
        if self._squash_say:
            tools = [self._without_say(tool) for tool in tools]
        return tools

    @staticmethod
    def _without_say(tool: dict[str, Any]) -> dict[str, Any]:
        """Return a deep copy of *tool* with its ``say`` parameter schema removed, if any."""
        tool = copy.deepcopy(tool)
        parameters = tool.get("function", {}).get("parameters", {})
        properties = parameters.get("properties")
        if isinstance(properties, dict):
            properties.pop("say", None)
        required = parameters.get("required")
        if isinstance(required, list) and "say" in required:
            required.remove("say")
        return tool

    def is_long_running(self, name: str) -> bool:
        """Whether *name* is a long-running tool (see :attr:`~palmimo_sdk.agent.tools.Tool.long_running`)."""
        cls = self._toolset.tool_models.get(name)
        return cls is not None and cls.long_running

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Dispatch *name* through the underlying toolset, honoring the allowlist and squash_say.

        A name outside the allowlist is reported as a non-raising,
        ``is_error=True`` :class:`~palmimo_sdk.agent.tools.ToolResult` (a
        malformed/hallucinated call must not crash the turn) rather than
        raised, mirroring the underlying toolset's own never-raises contract.
        """
        if self._allow is not None and name not in self._allow:
            return ToolResult(text=f"tool {name!r} is not available right now", is_error=True)
        call_args = dict(args)
        if self._squash_say:
            call_args.pop("say", None)
        return await self._toolset.call(name, call_args)

    async def cancel_running(self) -> None:
        """Passthrough to the underlying toolset's :meth:`~palmimo_sdk.agent.toolset.AgentToolSet.cancel_running`."""
        await self._toolset.cancel_running()

    def is_busy(self) -> bool:
        """Passthrough to the underlying toolset's :meth:`~palmimo_sdk.agent.toolset.AgentToolSet.is_busy`."""
        return self._toolset.is_busy()


__all__ = ["IDLE_TOOL_NAMES", "RESPOND_ONLY_TOOL_NAMES", "ToolView"]
