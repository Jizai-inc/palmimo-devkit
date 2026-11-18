"""EventRouter + BargeIn -- the session's one place that reads the server event stream.

:class:`EventRouter` is the sole consumer of :meth:`~..client.RealtimeClient.events`
-- one ``async for`` loop, one ``match`` statement, ordering explicit (see the
handler below). :class:`BargeIn` is the single entry point where human speech
silences current behavior; session-end stopping is a different thing, owned
by :class:`~..app.RealtimeSession._shutdown`.
"""

from __future__ import annotations

import base64
import sys
from typing import Any, Protocol

from .. import protocol
from ..client import RealtimeClientLike
from ..log import EventLog
from ..protocol import Response
from .audio import Playback
from .idle import IdlePacer


#: After its first line, an unknown event kind is logged again only on every
#: this-many-th occurrence, so a kind that keeps streaming stays visible even
#: if the process dies before the closing summary.
_UNKNOWN_RELOG_INTERVAL = 100


class BridgeLike(Protocol):
    """The subset of :class:`~..bridge.ToolBridge` this router/BargeIn needs."""

    async def interrupt(self) -> bool: ...

    async def handle(self, response: Response) -> None: ...


class UsageLike(Protocol):
    """The subset of the usage accumulator (built in ``app.py``) this router needs."""

    def add(self, usage: dict[str, Any]) -> None: ...


class SleepingLike(Protocol):
    """The subset of :class:`~..state.Sleeping` this router needs (read-only here)."""

    asleep: bool


class _SessionClosed(Exception):  # noqa: N818 -- internal control-flow signal, not a reported error
    """Raised by :meth:`EventRouter.run` when the server closed the socket.

    A normal way a session ends, not an error: the server hanging up (idle
    timeout, a network drop, the operator's own client-side action) makes
    :meth:`~..client.RealtimeClient.events` end its ``async for`` normally --
    but :class:`asyncio.TaskGroup` does NOT cancel its other children just
    because one task returns normally, so without raising here the mic,
    idle, and frame-push services would keep running against a closed socket
    until :class:`~..app.RealtimeSession`'s outer ``asyncio.timeout`` finally
    fires. Module-private by convention (this router owns detecting the
    close), but imported directly by ``app.py`` to add to its own
    ``except*`` list alongside :class:`~..app._Stop` and ``TimeoutError``.
    """


class BargeIn:
    """Silences current behavior the instant human speech is detected.

    Playback is killed immediately (ALSA has already buffered whatever was
    written, so a graceful stop would not be fast enough to read as barge-in),
    the toolset's in-flight motion is cancelled, and the idle pacer is
    touched so a tick doesn't land on top of the moment someone just started
    talking.

    Each trigger writes one ``barge_in`` line saying what the speech onset
    cut off, so how often the robot was interrupted -- by a bystander or a
    VAD false positive included -- can be counted from the log.
    """

    def __init__(
        self, playback: Playback, bridge: BridgeLike, pacer: IdlePacer, sleeping: SleepingLike, log: EventLog
    ) -> None:
        self._playback = playback
        self._bridge = bridge
        self._pacer = pacer
        self._sleeping = sleeping
        self._log = log

    async def trigger(self) -> None:
        cut = self._playback.interrupt()
        had_plan = await self._bridge.interrupt()
        self._pacer.touch()
        self._log.write(
            "barge_in",
            had_queued=cut.had_queued,
            recently_wrote=cut.recently_wrote,
            had_plan=had_plan,
            asleep=self._sleeping.asleep,
        )


class EventRouter:
    """Consumes the server event stream and dispatches each event exactly once."""

    name = "router"

    def __init__(
        self,
        client: RealtimeClientLike,
        playback: Playback,
        barge_in: BargeIn,
        bridge: BridgeLike,
        idle_pacer: IdlePacer,
        sleeping: SleepingLike,
        usage: UsageLike,
        log: EventLog,
    ) -> None:
        self._client = client
        self._playback = playback
        self._barge_in = barge_in
        self._bridge = bridge
        self._idle_pacer = idle_pacer
        self._sleeping = sleeping
        self._usage = usage
        self._log = log
        self._unknown_counts: dict[str, int] = {}

    async def run(self) -> None:
        """Consume events until the server closes the socket, then raise :class:`_SessionClosed`.

        See that exception's docstring for why a bare return would leave the
        other services running against a dead session. The
        ``unknown_event_summary`` line is written on every way out: the
        socket closing, an exception from the stream, or this task being
        cancelled (session timeout, a stop signal).
        """
        try:
            async for event in self._client.events():
                await self._dispatch(event)
        finally:
            if self._unknown_counts:
                self._log.write("unknown_event_summary", counts=dict(self._unknown_counts))
        raise _SessionClosed

    async def _dispatch(self, event: protocol.ServerEvent | protocol.Unknown) -> None:
        match event:
            case protocol.AudioDelta():
                self._playback.write(base64.b64decode(event.delta))
            case protocol.SpeechStarted():
                self._log.write("speech_started")
                await self._barge_in.trigger()
            case protocol.SpeechStopped():
                self._idle_pacer.touch()
                self._log.write("speech_stopped")
            case protocol.ResponseCreated():
                self._idle_pacer.busy = True
            case protocol.ResponseDone():
                self._idle_pacer.busy = False
                self._idle_pacer.touch()
                if event.response.usage:
                    self._usage.add(event.response.usage)
                await self._bridge.handle(event.response)
            case protocol.TranscriptCompleted():
                print(f"you     > {event.transcript}", flush=True)
                self._log.write("transcript", speaker="you", text=event.transcript)
            case protocol.TranscriptDone():
                print(f"palmimo > {event.transcript}", flush=True)
                self._log.write("transcript", speaker="palmimo", text=event.transcript)
            case protocol.ErrorEvent():
                print(f"!! {event.error}", file=sys.stderr, flush=True)
                self._log.write("server_error", error=event.error)
            case protocol.Unknown():
                # Never silently swallowed, but not one line per streamed
                # delta either: the first of each kind in full, then counted.
                count = self._unknown_counts.get(event.type, 0) + 1
                self._unknown_counts[event.type] = count
                if count == 1:
                    self._log.write("unknown_event", event_type=event.type)
                elif count % _UNKNOWN_RELOG_INTERVAL == 0:
                    self._log.write("unknown_event", event_type=event.type, occurrence=count)


__all__ = ["BargeIn", "BridgeLike", "EventRouter", "SleepingLike", "UsageLike", "_SessionClosed"]
