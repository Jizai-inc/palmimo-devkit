"""What the AI mode's model sees: head-camera frames sent into the Realtime conversation.

Three ways a frame reaches the model, all through one `LiveFrame`:

- `FramePusher` replaces the frame every `FRAME_SECONDS` while the robot is
  quiet, so the model always has a recent view of the room.
- `FramePusher.nudge` sends one at once when a reflex notices a new face, so
  the model knows someone arrived before they speak.
- The `see` tool lets the model ask for a fresh photo when it needs one.

`LiveFrame` keeps a single image in the conversation: each new frame deletes
the one before it. Images are billed as input tokens and Realtime re-bills
the whole context every turn, so a trail of old frames would make every turn
dearer while telling the model nothing new. Adapted from the companion
agent's `realtime/services/frames.py`; an example carries its own copy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from ..telemetry import Telemetry
from .client import RealtimeClientLike
from .protocol import ItemCreate, ItemDelete


_LOG = logging.getLogger(__name__)

#: Seconds between frames sent while the robot is quiet. The main cost dial:
#: each frame is a few hundred input tokens, re-billed (mostly cached) on every turn.
FRAME_SECONDS: float = 10.0
#: How soon a frame that was skipped because the robot was busy is tried again.
BUSY_RETRY_S: float = 1.0

#: The tool the model calls for a fresh photo.
SEE_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "see",
    "description": (
        "Take a fresh photo with your head camera. Use it when you need to look at something now, "
        "for example when someone asks what you can see or shows you something. "
        "The photo arrives as the next image in the conversation."
    ),
    "parameters": {"type": "object", "properties": {}},
}

#: Added to every mode's prompt while frames are on, so a prompt edited on the
#: homepage still tells the model what the images are.
VISION_NOTE = (
    "\n\n# Camera\n"
    "Images in this conversation come from your head camera and show what is in front of you. "
    "The latest one may be a few seconds old; call `see` when you need to look right now. "
    "Mention what you see only when it helps the conversation."
)


class LiveFrame:
    """The single camera frame the model currently holds.

    Tracks only the id of the item it last created; a delete the API rejects
    leaves at most one stale frame, because the next push moves on regardless.
    """

    def __init__(self, jpeg: Callable[[], bytes | None]) -> None:
        self._jpeg = jpeg
        self._previous: str | None = None
        self._count = 0

    def newest(self) -> bytes | None:
        """The camera's newest JPEG, or `None` with no frame ready."""
        return self._jpeg()

    async def push(self, client: RealtimeClientLike) -> bool:
        """Replace the model's frame with the newest one; `False` (nothing sent) with no frame ready."""
        jpeg = self.newest()
        if jpeg is None:
            return False
        await self.replace(client, jpeg)
        return True

    async def replace(self, client: RealtimeClientLike, jpeg: bytes) -> None:
        """Replace the model's frame with *jpeg*."""
        self._count += 1
        item_id = f"frame_{self._count:06d}"
        if self._previous is not None:
            await client.send(ItemDelete(item_id=self._previous))
        await client.send(ItemCreate.image(jpeg, item_id=item_id))
        self._previous = item_id


class FramePusher:
    """Sends a frame every `FRAME_SECONDS`, and at once when nudged, skipping while the robot is busy.

    A frame never asks the model to respond: it is there for the next turn.
    Runs as one task of the AI session and only ends by cancellation.
    """

    def __init__(
        self,
        frame: LiveFrame,
        client: RealtimeClientLike,
        *,
        busy: Callable[[], bool],
        telemetry: Telemetry | None = None,
        interval: float = FRAME_SECONDS,
        busy_retry: float = BUSY_RETRY_S,
    ) -> None:
        self._frame = frame
        self._client = client
        self._busy = busy
        self._telemetry = telemetry
        self._interval = interval
        self._busy_retry = busy_retry
        self._nudged = asyncio.Event()
        self.pushed = 0

    def nudge(self) -> None:
        """Send a frame as soon as the robot is quiet (a reflex just noticed a new face)."""
        self._nudged.set()

    async def run(self) -> None:
        reason = "start"
        while True:
            if self._busy():
                await asyncio.sleep(self._busy_retry)
                continue
            await self._send(reason)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._interval):
                    await self._nudged.wait()
            reason = "face" if self._nudged.is_set() else "periodic"
            self._nudged.clear()

    async def _send(self, reason: str) -> None:
        if await self._frame.push(self._client):
            self.pushed += 1
            _LOG.debug("AI frame sent (%s)", reason)
            if self._telemetry is not None:
                self._telemetry.event("frame_push", reason=reason, pushed=self.pushed)


__all__ = ["BUSY_RETRY_S", "FRAME_SECONDS", "SEE_TOOL", "VISION_NOTE", "FramePusher", "LiveFrame"]
