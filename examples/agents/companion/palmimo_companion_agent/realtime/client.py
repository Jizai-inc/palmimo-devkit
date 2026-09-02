"""RealtimeClient -- owns the websocket connection to the OpenAI Realtime API.

Everything above this module speaks typed :mod:`.protocol` models; this is
the only place that touches the ``websockets`` connection and JSON
(de)serialization. :class:`RealtimeClientLike` is the structural type every
service and :class:`~.bridge.ToolBridge` actually depends on, so a test can
substitute a recorder without a real socket.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Protocol

import websockets

from .protocol import ClientEvent, ServerEvent, Unknown, dump, parse_server_event


class RealtimeClientLike(Protocol):
    """The subset of :class:`RealtimeClient` every service/bridge depends on."""

    async def send(self, event: ClientEvent) -> None: ...

    def events(self) -> AsyncIterator[ServerEvent | Unknown]: ...


class RealtimeClient:
    """A connected Realtime API session: typed send, typed event stream."""

    def __init__(self, socket: object) -> None:
        self._socket = socket

    @classmethod
    @contextlib.asynccontextmanager
    async def connect(cls, *, model: str, api_key: str) -> AsyncIterator[RealtimeClient]:
        """Open the websocket for *model*, yielding a connected client for the ``async with`` body."""
        url = f"wss://api.openai.com/v1/realtime?model={model}"
        async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {api_key}"}) as socket:
            yield cls(socket)

    async def send(self, event: ClientEvent) -> None:
        """Serialize and send one client event."""
        await self._socket.send(dump(event))  # type: ignore[attr-defined]

    async def events(self) -> AsyncIterator[ServerEvent | Unknown]:
        """Yield parsed server events until the socket closes."""
        async for raw in self._socket:  # type: ignore[attr-defined]
            yield parse_server_event(raw)


__all__ = ["RealtimeClient", "RealtimeClientLike"]
