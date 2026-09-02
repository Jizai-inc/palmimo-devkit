"""Tests for :mod:`palmimo_companion_agent.realtime.services.router` -- EventRouter + BargeIn."""

from __future__ import annotations

from typing import Any

import pytest

from palmimo_companion_agent.realtime.log import EventLog
from palmimo_companion_agent.realtime.protocol import ErrorEvent, Response, ResponseDone, SpeechStarted, Unknown
from palmimo_companion_agent.realtime.services.idle import IdlePacer
from palmimo_companion_agent.realtime.services.router import BargeIn, EventRouter, _SessionClosed
from palmimo_companion_agent.realtime.state import Sleeping


class _Playback:
    def __init__(self) -> None:
        self.interrupts = 0

    def write(self, data: bytes) -> None: ...

    def interrupt(self) -> None:
        self.interrupts += 1


class _Bridge:
    def __init__(self) -> None:
        self.interrupts = 0
        self.handled: list[Response] = []

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def handle(self, response: Response) -> None:
        self.handled.append(response)


class _Usage:
    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []

    def add(self, usage: dict[str, Any]) -> None:
        self.added.append(usage)


class _RecordingLog(EventLog):
    def __init__(self) -> None:
        super().__init__(None)
        self.entries: list[tuple[str, dict[str, Any]]] = []

    def write(self, kind: str, **fields: Any) -> None:
        self.entries.append((kind, fields))


async def test_barge_in_interrupts_playback_and_bridge_and_touches_the_pacer(fake_client: type) -> None:
    """Silencing the voice is not stopping the robot -- see BargeIn's own docstring."""
    playback = _Playback()
    bridge = _Bridge()
    pacer = IdlePacer()

    await BargeIn(playback, bridge, pacer).trigger()

    assert playback.interrupts == 1
    assert bridge.interrupts == 1


async def test_speech_started_event_routes_through_barge_in(fake_client: type) -> None:
    client = fake_client([SpeechStarted()])
    playback = _Playback()
    bridge = _Bridge()
    pacer = IdlePacer()
    barge_in = BargeIn(playback, bridge, pacer)
    router = EventRouter(client, playback, barge_in, bridge, pacer, Sleeping(), _Usage(), EventLog.open(None))

    with pytest.raises(_SessionClosed):
        await router.run()

    assert playback.interrupts == 1
    assert bridge.interrupts == 1


async def test_response_done_updates_the_pacer_and_usage_and_hands_off_to_the_bridge(fake_client: type) -> None:
    response = Response(id="r1", usage={"input_token_details": {"text_tokens": 3}})
    client = fake_client([ResponseDone(response=response)])
    playback = _Playback()
    bridge = _Bridge()
    pacer = IdlePacer()
    pacer.busy = True
    usage = _Usage()
    router = EventRouter(
        client, playback, BargeIn(playback, bridge, pacer), bridge, pacer, Sleeping(), usage, EventLog.open(None)
    )

    with pytest.raises(_SessionClosed):
        await router.run()

    assert pacer.busy is False
    assert usage.added == [{"input_token_details": {"text_tokens": 3}}]
    assert bridge.handled == [response]


async def test_unknown_events_are_logged_not_silently_dropped(fake_client: type) -> None:
    """An unhandled-but-real event kind must be visible, not vanish."""
    client = fake_client([Unknown(type="rate_limits.updated")])
    playback = _Playback()
    bridge = _Bridge()
    pacer = IdlePacer()
    log = _RecordingLog()
    router = EventRouter(client, playback, BargeIn(playback, bridge, pacer), bridge, pacer, Sleeping(), _Usage(), log)

    with pytest.raises(_SessionClosed):
        await router.run()

    assert log.entries == [("unknown_event", {"event_type": "rate_limits.updated"})]


# ----------------------------------------------------------------------
# Session-close: a normal end, not an error, but not a silent hang either
# ----------------------------------------------------------------------


async def test_the_socket_closing_ends_the_router_with_session_closed(fake_client: type) -> None:
    """A TaskGroup does not cancel its other children just because one task returns
    normally -- see _SessionClosed's own docstring -- so the router must raise, not
    just fall out of its `async for` when the server hangs up."""
    client = fake_client([])  # an already-exhausted event stream, like a closed socket
    playback = _Playback()
    bridge = _Bridge()
    pacer = IdlePacer()
    router = EventRouter(
        client, playback, BargeIn(playback, bridge, pacer), bridge, pacer, Sleeping(), _Usage(), EventLog.open(None)
    )

    with pytest.raises(_SessionClosed):
        await router.run()


async def test_the_socket_closing_after_some_events_still_raises_session_closed(fake_client: type) -> None:
    client = fake_client([ErrorEvent(error={"message": "oops"})])
    playback = _Playback()
    bridge = _Bridge()
    pacer = IdlePacer()
    router = EventRouter(
        client, playback, BargeIn(playback, bridge, pacer), bridge, pacer, Sleeping(), _Usage(), EventLog.open(None)
    )

    with pytest.raises(_SessionClosed):
        await router.run()
