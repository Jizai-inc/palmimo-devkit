"""Tests for :mod:`palmimo_companion_agent.realtime.client` -- RealtimeClient's send/events, no real socket."""

from __future__ import annotations

from typing import Any

from palmimo_companion_agent.realtime.client import RealtimeClient
from palmimo_companion_agent.realtime.protocol import AudioAppend, SpeechStarted


class _FakeSocket:
    def __init__(self, incoming: list[str]) -> None:
        self.sent: list[str] = []
        self._incoming = incoming

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    def __aiter__(self) -> Any:
        async def _gen() -> Any:
            for raw in self._incoming:
                yield raw

        return _gen()


async def test_send_serializes_the_client_event_through_protocol_dump() -> None:
    socket = _FakeSocket([])
    client = RealtimeClient(socket)

    await client.send(AudioAppend(audio_b64="YWJj"))

    assert socket.sent == ['{"type": "input_audio_buffer.append", "audio": "YWJj"}']


async def test_events_parses_every_frame_from_the_socket() -> None:
    socket = _FakeSocket(['{"type": "input_audio_buffer.speech_started"}'])
    client = RealtimeClient(socket)

    events = [event async for event in client.events()]

    assert events == [SpeechStarted()]
