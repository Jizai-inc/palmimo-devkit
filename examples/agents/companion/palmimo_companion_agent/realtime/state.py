"""Sleeping -- this runtime's own mirror of whether the robot is currently asleep.

Not a query of the robot's own state (the SDK facade has no ``is_asleep``): a
plain flag :class:`~.bridge.ToolBridge` flips after a completed ``sleep`` /
``wake_up`` tool call. This is a SEPARATE class from
:class:`palmimo_companion_agent.pipeline.sleeping.Sleeping` -- that one is a
:class:`~palmimo_companion_agent.pipeline.history.History`-subscriber built
around ``ToolExecEvent``, and ``History`` is pipeline-only infrastructure this
runtime does not have (see the package README's "one character, two
runtimes" split). This module is realtime's own, observed directly from
:class:`~.bridge.ToolBridge`'s plan-execution loop instead of through a
history subscription.

Mirror-and-recovery contract: :meth:`observe` only reacts to a call whose
result was neither an error nor interrupted, so the mirror can drift from the
robot's true state if a long-running ``wake_up`` is cancelled mid-glide by a
barge-in -- the robot may already be moving out of sleep while this still
reports ``asleep=True``. That drift is not chased in between; it self-heals
on the next successful ``sleep`` or ``wake_up`` call, which re-syncs the mirror.
"""

from __future__ import annotations


#: Tool names this mirror watches for. Shared with the SDK's own vocabulary
#: (and this agent's choreographed overrides of it) via COMPANION_TOOL_MODELS.
_SLEEP_TOOL_NAME = "sleep"
_WAKE_TOOL_NAME = "wake_up"


class Sleeping:
    """Mirrors whether the robot is asleep, updated by :class:`~.bridge.ToolBridge` after each clean tool result."""

    def __init__(self) -> None:
        self.asleep = False

    def observe(self, tool_name: str) -> None:
        """Flip the mirror if *tool_name* is ``sleep`` or ``wake_up``.

        Only called for a call whose result was neither an error nor
        interrupted -- see the module docstring's recovery contract.
        """
        if tool_name == _SLEEP_TOOL_NAME:
            self.asleep = True
        elif tool_name == _WAKE_TOOL_NAME:
            self.asleep = False


__all__ = ["Sleeping"]
