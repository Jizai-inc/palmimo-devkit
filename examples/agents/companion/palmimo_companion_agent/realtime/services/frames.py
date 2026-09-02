"""LiveFrame + FramePusher -- the model's eyes: one camera frame in the conversation, replaced on a timer.

Realtime re-bills the whole context on every turn, so a trail of appended
frames would make each later turn dearer than the last. :class:`LiveFrame`
keeps exactly one frame item live by deleting the previous one before
creating the next; :class:`FramePusher` is the periodic loop that calls it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..client import RealtimeClientLike
from ..log import EventLog
from ..protocol import ItemCreate, ItemDelete
from ..state import Sleeping
from .idle import IdlePacer


class LiveFrame:
    """Mirror of the single conversation item currently holding the model's view of the room.

    Mirror-with-recovery contract: this only tracks the item id it last
    created -- it does not confirm the delete actually landed server-side.
    If the API rejects a delete (e.g. the item was already gone), that
    surfaces as an ``error`` event, which :class:`~.router.EventRouter`
    already logs -- this class does not need to react to it specially,
    because the NEXT :meth:`push` unconditionally replaces the tracked id
    with a new one regardless of whether the previous delete succeeded. The
    failure mode this leaves is at most one stale frame item lingering
    server-side after a rejected delete, never an accumulating trail: the
    tracker always advances, so cost never compounds the way an un-deleted
    trail would.
    """

    def __init__(self, camera: Any) -> None:
        self._camera = camera
        self._previous: str | None = None
        self._count = 0
        self.pushed = 0

    async def push(self, client: RealtimeClientLike) -> None:
        """Replace the live frame, or do nothing if the camera had none ready."""
        jpeg = await asyncio.to_thread(self._encode)
        if jpeg is None:
            return
        self._count += 1
        item_id = f"frame_{self._count:06d}"
        if self._previous is not None:
            await client.send(ItemDelete(item_id=self._previous))
        await client.send(ItemCreate.image(jpeg, item_id=item_id))
        self._previous = item_id
        self.pushed += 1

    def _encode(self) -> bytes | None:
        """Newest drained frame as JPEG. Runs off the loop: encoding is not free."""
        import cv2  # lazy, matching the capture tool's own import style

        frame = self._camera.latest(timeout=0.5) if self._camera is not None else None
        if frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame)
        return bytes(buf.tobytes()) if ok else None


class FramePusher:
    """Periodically pushes a fresh camera frame, skipping while busy or asleep."""

    name = "frames"

    def __init__(
        self,
        client: RealtimeClientLike,
        frame: LiveFrame,
        pacer: IdlePacer,
        sleeping: Sleeping,
        frame_seconds: float,
        log: EventLog,
    ) -> None:
        self._client = client
        self._frame = frame
        self._pacer = pacer
        self._sleeping = sleeping
        self._frame_seconds = frame_seconds
        self._log = log

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._frame_seconds)
            if self._pacer.busy or self._sleeping.asleep:
                continue
            before = self._frame.pushed
            await self._frame.push(self._client)
            if self._frame.pushed > before:
                self._log.write("frame_push", pushed=self._frame.pushed)


__all__ = ["FramePusher", "LiveFrame"]
