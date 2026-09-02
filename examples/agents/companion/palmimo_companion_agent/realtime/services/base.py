"""Service -- the common shape every realtime background loop implements.

:class:`~..app.RealtimeSession` runs every :class:`Service` inside one
``asyncio.TaskGroup``: a service's :meth:`~Service.run` is expected to run
until the task group cancels it (session end), and raising ends the WHOLE
session -- a service that hits an unrecoverable error should let the
exception propagate rather than swallow it and go quiet.

Deliberately no ``stop`` parameter: session-level stop lives only in
``app.py`` (the ``asyncio.timeout`` / signal-driven ``TaskGroup`` cancels
every service together), so a service never needs to poll a flag of its own.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Service(Protocol):
    """One background loop the session runs for its whole lifetime."""

    name: str

    async def run(self) -> None:
        """Run until cancelled. Raising ends the whole session."""
        ...


__all__ = ["Service"]
