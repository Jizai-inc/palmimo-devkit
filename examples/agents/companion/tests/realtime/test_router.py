"""Tests for :mod:`palmimo_companion_agent.realtime.services.router` -- EventRouter + BargeIn."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from palmimo_companion_agent.realtime.log import EventLog
from palmimo_companion_agent.realtime.protocol import (
    ErrorEvent,
    Response,
    ResponseDone,
    ServerEvent,
    SpeechStarted,
    SpeechStopped,
    Unknown,
)
from palmimo_companion_agent.realtime.services.audio import Playback, PlaybackInterruptResult
from palmimo_companion_agent.realtime.services.idle import IdlePacer
from palmimo_companion_agent.realtime.services.router import BargeIn, EventRouter, _SessionClosed
from palmimo_companion_agent.realtime.state import Sleeping


class _Playback:
    def __init__(self, *, had_queued: bool = False, recently_wrote: bool = False) -> None:
        self.interrupts = 0
        self._result = PlaybackInterruptResult(had_queued=had_queued, recently_wrote=recently_wrote)

    def write(self, data: bytes) -> None: ...

    def interrupt(self) -> PlaybackInterruptResult:
        self.interrupts += 1
        return self._result


def _playback(fake: _Playback) -> Playback:
    return cast(Playback, fake)


class _Bridge:
    def __init__(self, *, had_plan: bool = False) -> None:
        self.interrupts = 0
        self.handled: list[Response] = []
        self._had_plan = had_plan

    async def interrupt(self) -> bool:
        self.interrupts += 1
        return self._had_plan

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

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [fields for entry_kind, fields in self.entries if entry_kind == kind]


def _router(client: Any, log: EventLog, playback: Playback | None = None, bridge: _Bridge | None = None) -> EventRouter:
    playback = playback or _playback(_Playback())
    bridge = bridge or _Bridge()
    pacer = IdlePacer()
    barge_in = BargeIn(playback, bridge, pacer, Sleeping(), log)
    return EventRouter(client, playback, barge_in, bridge, pacer, Sleeping(), _Usage(), log)


async def test_barge_in_interrupts_playback_and_bridge_and_touches_the_pacer(fake_client: type) -> None:
    """Silencing the voice is not stopping the robot -- see BargeIn's own docstring."""
    fake_playback = _Playback()
    playback = _playback(fake_playback)
    bridge = _Bridge()
    pacer = IdlePacer()

    await BargeIn(playback, bridge, pacer, Sleeping(), EventLog.open(None)).trigger()

    assert fake_playback.interrupts == 1
    assert bridge.interrupts == 1


async def test_speech_started_event_routes_through_barge_in(fake_client: type) -> None:
    client = fake_client([SpeechStarted()])
    fake_playback = _Playback()
    playback = _playback(fake_playback)
    bridge = _Bridge()
    pacer = IdlePacer()
    barge_in = BargeIn(playback, bridge, pacer, Sleeping(), EventLog.open(None))
    router = EventRouter(client, playback, barge_in, bridge, pacer, Sleeping(), _Usage(), EventLog.open(None))

    with pytest.raises(_SessionClosed):
        await router.run()

    assert fake_playback.interrupts == 1
    assert bridge.interrupts == 1


async def test_response_done_updates_the_pacer_and_usage_and_hands_off_to_the_bridge(fake_client: type) -> None:
    response = Response(id="r1", usage={"input_token_details": {"text_tokens": 3}})
    client = fake_client([ResponseDone(response=response)])
    playback = _playback(_Playback())
    bridge = _Bridge()
    pacer = IdlePacer()
    pacer.busy = True
    usage = _Usage()
    router = EventRouter(
        client,
        playback,
        BargeIn(playback, bridge, pacer, Sleeping(), EventLog.open(None)),
        bridge,
        pacer,
        Sleeping(),
        usage,
        EventLog.open(None),
    )

    with pytest.raises(_SessionClosed):
        await router.run()

    assert pacer.busy is False
    assert usage.added == [{"input_token_details": {"text_tokens": 3}}]
    assert bridge.handled == [response]


async def test_unknown_events_are_logged_not_silently_dropped(fake_client: type) -> None:
    """An unhandled-but-real event kind must be visible, not vanish."""
    client = fake_client([Unknown(type="rate_limits.updated")])
    playback = _playback(_Playback())
    bridge = _Bridge()
    pacer = IdlePacer()
    log = _RecordingLog()
    router = EventRouter(
        client, playback, BargeIn(playback, bridge, pacer, Sleeping(), log), bridge, pacer, Sleeping(), _Usage(), log
    )

    with pytest.raises(_SessionClosed):
        await router.run()

    assert log.of_kind("unknown_event") == [{"event_type": "rate_limits.updated"}]


# ----------------------------------------------------------------------
# Session-close: a normal end, not an error, but not a silent hang either
# ----------------------------------------------------------------------


async def test_the_socket_closing_ends_the_router_with_session_closed(fake_client: type) -> None:
    """A TaskGroup does not cancel its other children just because one task returns
    normally -- see _SessionClosed's own docstring -- so the router must raise, not
    just fall out of its `async for` when the server hangs up."""
    client = fake_client([])  # an already-exhausted event stream, like a closed socket
    playback = _playback(_Playback())
    bridge = _Bridge()
    pacer = IdlePacer()
    router = EventRouter(
        client,
        playback,
        BargeIn(playback, bridge, pacer, Sleeping(), EventLog.open(None)),
        bridge,
        pacer,
        Sleeping(),
        _Usage(),
        EventLog.open(None),
    )

    with pytest.raises(_SessionClosed):
        await router.run()


async def test_the_socket_closing_after_some_events_still_raises_session_closed(fake_client: type) -> None:
    client = fake_client([ErrorEvent(error={"message": "oops"})])
    playback = _playback(_Playback())
    bridge = _Bridge()
    pacer = IdlePacer()
    router = EventRouter(
        client,
        playback,
        BargeIn(playback, bridge, pacer, Sleeping(), EventLog.open(None)),
        bridge,
        pacer,
        Sleeping(),
        _Usage(),
        EventLog.open(None),
    )

    with pytest.raises(_SessionClosed):
        await router.run()


# ----------------------------------------------------------------------
# Barge-in log lines: what the speech onset cut off
# ----------------------------------------------------------------------


async def test_barge_in_logs_what_it_cut_off() -> None:
    playback = _playback(_Playback(had_queued=True, recently_wrote=True))
    log = _RecordingLog()

    await BargeIn(playback, _Bridge(had_plan=True), IdlePacer(), Sleeping(), log).trigger()

    assert log.entries == [
        ("barge_in", {"had_queued": True, "recently_wrote": True, "had_plan": True, "asleep": False})
    ]


async def test_barge_in_logs_the_asleep_state_when_nothing_was_cut_off() -> None:
    sleeping = Sleeping()
    sleeping.asleep = True
    log = _RecordingLog()

    await BargeIn(_playback(_Playback()), _Bridge(), IdlePacer(), sleeping, log).trigger()

    assert log.entries == [
        ("barge_in", {"had_queued": False, "recently_wrote": False, "had_plan": False, "asleep": True})
    ]


async def test_speech_onset_and_end_are_logged_around_the_barge_in(fake_client: type) -> None:
    fake_playback = _Playback()
    bridge = _Bridge()
    log = _RecordingLog()
    router = _router(fake_client([SpeechStarted(), SpeechStopped()]), log, _playback(fake_playback), bridge)

    with pytest.raises(_SessionClosed):
        await router.run()

    assert [kind for kind, _ in log.entries] == ["speech_started", "barge_in", "speech_stopped"]
    assert fake_playback.interrupts == 1
    assert bridge.interrupts == 1


# ----------------------------------------------------------------------
# Unknown events: first in full, then counted, summarized on every exit
# ----------------------------------------------------------------------


async def test_repeat_unknown_events_are_counted_and_relogged_every_hundredth(fake_client: type) -> None:
    events: list[ServerEvent | Unknown] = [Unknown(type="rate_limits.updated")] * 205
    events.insert(1, Unknown(type="response.output_item.added"))
    log = _RecordingLog()

    with pytest.raises(_SessionClosed):
        await _router(fake_client(events), log).run()

    assert log.of_kind("unknown_event") == [
        {"event_type": "rate_limits.updated"},
        {"event_type": "response.output_item.added"},
        {"event_type": "rate_limits.updated", "occurrence": 100},
        {"event_type": "rate_limits.updated", "occurrence": 200},
    ]
    assert log.of_kind("unknown_event_summary") == [
        {"counts": {"rate_limits.updated": 205, "response.output_item.added": 1}}
    ]


async def test_no_unknown_summary_without_unknown_events(fake_client: type) -> None:
    log = _RecordingLog()

    with pytest.raises(_SessionClosed):
        await _router(fake_client([SpeechStopped()]), log).run()

    assert log.of_kind("unknown_event_summary") == []


class _StallingClient:
    """Yields *events*, then either raises *error* or blocks as an open, quiet socket would."""

    def __init__(self, events: list[ServerEvent | Unknown], error: Exception | None = None) -> None:
        self._events = events
        self._error = error
        self.drained = asyncio.Event()

    async def send(self, event: Any) -> None: ...

    def events(self) -> AsyncIterator[ServerEvent | Unknown]:
        async def _gen() -> AsyncIterator[ServerEvent | Unknown]:
            for event in self._events:
                yield event
            self.drained.set()
            if self._error is not None:
                raise self._error
            await asyncio.Event().wait()

        return _gen()


async def test_cancelling_the_router_still_writes_the_unknown_summary() -> None:
    """Session timeout and stop signals end the router by cancelling its task."""
    client = _StallingClient([Unknown(type="rate_limits.updated")] * 2)
    log = _RecordingLog()
    task = asyncio.ensure_future(_router(client, log).run())
    await client.drained.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert log.of_kind("unknown_event_summary") == [{"counts": {"rate_limits.updated": 2}}]


async def test_a_failing_event_stream_still_writes_the_unknown_summary() -> None:
    client = _StallingClient([Unknown(type="rate_limits.updated")], error=ConnectionError("dropped"))
    log = _RecordingLog()

    with pytest.raises(ConnectionError):
        await _router(client, log).run()

    assert log.of_kind("unknown_event_summary") == [{"counts": {"rate_limits.updated": 1}}]
