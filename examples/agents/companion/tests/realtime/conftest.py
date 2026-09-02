"""Shared fakes for tests/realtime/ -- no websocket, no API key, no camera anywhere below.

:class:`FakeClient` implements :class:`~palmimo_companion_agent.realtime.client.RealtimeClientLike`:
it records every :class:`~palmimo_companion_agent.realtime.protocol.ClientEvent`
sent (as typed model instances, never dict-key digging) and replays a
scripted list of server events to whatever consumes :meth:`~FakeClient.events`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from palmimo_companion_agent.realtime.protocol import ClientEvent, ServerEvent, Unknown


class FakeClient:
    """Records sent events; replays a scripted server-event stream."""

    def __init__(self, events: list[ServerEvent | Unknown] | None = None) -> None:
        self.sent: list[ClientEvent] = []
        self._events = events or []

    async def send(self, event: ClientEvent) -> None:
        self.sent.append(event)

    def events(self) -> AsyncIterator[ServerEvent | Unknown]:
        async def _gen() -> AsyncIterator[ServerEvent | Unknown]:
            for event in self._events:
                yield event

        return _gen()


@pytest.fixture
def fake_client() -> type[FakeClient]:
    """The :class:`FakeClient` class itself, so a test can construct it with its own scripted events."""
    return FakeClient
