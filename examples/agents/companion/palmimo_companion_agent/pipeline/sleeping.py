"""Sleeping -- a small mirror of whether the robot is currently asleep.

Not a query of the robot's own state (the SDK facade has no ``is_asleep``):
a subscriber-driven mirror built by watching :class:`~.history.History`'s
``ToolExecEvent`` stream for a completed ``sleep`` / ``wake_up`` call. Wired
as a :meth:`~.history.History.subscribe` callback from
:mod:`palmimo_companion_agent.pipeline.wiring`, so nothing upstream of
:class:`~.conductor.Conductor` needs to know this exists.

Mirror-and-recovery contract: because this only reacts to a COMPLETED
(non-interrupted, non-error) call, the mirror can drift from the robot's
true state if a long-running ``wake_up`` is interrupted mid-glide (see
``WakeUp.long_running=True`` in :mod:`~palmimo_companion_agent.core.tools`)
-- the robot may already be moving out of sleep while this still reports
``asleep=True``. That drift is not chased in between; it self-heals on the
next successful ``sleep`` or ``wake_up`` call, which re-syncs the mirror.
"""

from __future__ import annotations

from .dispatch import is_interrupted_result
from .history import Event, ToolExecEvent


#: Tool names this mirror watches for -- both the SDK's stock tools and this
#: agent's choreographed overrides share these names (see COMPANION_TOOL_MODELS).
_SLEEP_TOOL_NAME = "sleep"
_WAKE_TOOL_NAME = "wake_up"


class Sleeping:
    """Mirrors whether the robot is asleep, by observing executed sleep/wake_up tool results."""

    def __init__(self) -> None:
        self._asleep = False

    @property
    def asleep(self) -> bool:
        """Whether the mirror currently believes the robot is asleep."""
        return self._asleep

    def observe(self, event: Event) -> None:
        """History subscriber: update the mirror from a completed ``ToolExecEvent``.

        Only a clean result -- ``event.error`` False (see
        :class:`~.history.ToolExecEvent`'s own docstring: this covers both a
        local dispatch-side failure and a ``ToolResult.is_error`` the tool
        itself set, however the result text happens to be spelled), and not
        interrupted (see :func:`~.dispatch.is_interrupted_result`) -- flips
        the flag. A failed or interrupted sleep/wake_up call leaves the
        mirror exactly as it was (see the module docstring's recovery
        contract).
        """
        if not isinstance(event, ToolExecEvent):
            return
        if event.name not in (_SLEEP_TOOL_NAME, _WAKE_TOOL_NAME):
            return
        if event.error or is_interrupted_result(event.result):
            return
        self._asleep = event.name == _SLEEP_TOOL_NAME


__all__ = ["Sleeping"]
