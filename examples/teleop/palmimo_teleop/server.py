"""FastAPI app assembly: routes, static files, and the lifespan that starts/stops the
robot loop and camera capture alongside the server.

`create_app` is the sole entry point: it wires together an already-built
`Palmimo`, `PilotSession`, `RobotLoop`, and `VideoStream` (assembled in
`main.py`) into the routes the frontend talks to. Kept separate from
`main.py` so the FastAPI/HTTP layer never has to know about `typer` or CLI
option parsing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from palmimo_sdk import Palmimo

from .control import ALLOWED_GESTURES, ALLOWED_MOVES, ALLOWED_ROTATES, clamp_neck
from .robot_loop import RobotLoop
from .session import PilotSession
from .video import MJPEG_BOUNDARY, VideoStream, mjpeg_generator


_LOG = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: How often the `@2Hz` status broadcast fires.
STATUS_BROADCAST_INTERVAL_S: float = 0.5


class _ConnectionRegistry:
    """Every open `/api/control` WebSocket, for the periodic status broadcast.

    A plain dict keyed by the socket object rather than by role -- both
    pilot and viewers receive the same broadcast, so there is nothing role
    differentiates here.
    """

    def __init__(self) -> None:
        self._sockets: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def add(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._sockets.add(websocket)

    async def remove(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._sockets.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._sockets)
        for websocket in targets:
            with contextlib.suppress(Exception):
                await websocket.send_json(payload)


def _validate_pilot_message(message: object) -> tuple[str | None, int, float, float] | None:
    """Validate one pilot control frame.

    *message* is untyped `object`, not `dict[str, Any]`: it is whatever
    `json.loads` produced from an untrusted client, which for a bare JSON
    array or string is not a dict at all -- calling `.get` on it would raise
    `AttributeError` and kill the WebSocket handler, so every shape is
    checked before use rather than assumed.

    Returns:
        tuple, optional: `(move, rotate, neck_pitch, neck_yaw)` if the frame
            is well-formed, else `None` -- the caller drops the frame
            entirely (unknown `move`, or out-of-set `rotate`, is rejected
            outright, since those select a gait; neck is clamped rather than
            rejected, since no finite neck value is unsafe).
    """
    if not isinstance(message, dict):
        return None
    move = message.get("move")
    if move is not None and move not in ALLOWED_MOVES:
        return None
    rotate = message.get("rotate", 0)
    # `bool` is a subclass of `int` in Python, so `True in {-1, 0, 1}` is
    # `True` (`True == 1`) -- rejected explicitly rather than silently
    # accepted as rotate=1.
    if isinstance(rotate, bool) or rotate not in ALLOWED_ROTATES:
        return None
    neck = message.get("neck")
    if neck is None:
        neck = {}
    if not isinstance(neck, dict):
        return None
    try:
        pitch = clamp_neck(float(neck.get("pitch", 0.0)))
        yaw = clamp_neck(float(neck.get("yaw", 0.0)))
    except (TypeError, ValueError):
        return None
    return move, rotate, pitch, yaw


def _validate_play_message(message: dict[str, Any]) -> str | None:
    """Validate a `{"play": ...}` gesture-playback message.

    Returns:
        str, optional: the gesture name if it is in `ALLOWED_GESTURES`, else
            `None` -- the caller drops the message entirely, same as an
            unrecognized `move`.
    """
    name = message.get("play")
    if name not in ALLOWED_GESTURES:
        return None
    return name


def create_app(
    robot: Palmimo,
    session: PilotSession,
    robot_loop: RobotLoop,
    video: VideoStream,
) -> FastAPI:
    """Assemble the FastAPI app around an already-constructed robot loop and video stream.

    Args:
        robot (Palmimo): The facade the lifespan connects/disconnects.
        session (PilotSession): Shared pilot state; only the WebSocket handler writes to it.
        robot_loop (RobotLoop): Drives `robot` from `session`; started/stopped by the lifespan.
        video (VideoStream): Drives head camera capture; started/stopped by the lifespan.

    Returns:
        FastAPI: The assembled app, ready for `uvicorn.run`.
    """
    registry = _ConnectionRegistry()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if robot.has_connectable_resource:
            robot.connect()
        robot_loop.start()
        video.start()
        broadcast_task = asyncio.create_task(_broadcast_loop(robot, session, robot_loop, video, registry))
        try:
            yield
        finally:
            broadcast_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await broadcast_task
            video.stop()
            robot_loop.shutdown()
            if robot.has_connectable_resource:
                robot.disconnect()

    app = FastAPI(lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def viewer_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "viewer.html")

    @app.get("/pilot")
    async def pilot_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "pilot.html")

    @app.get("/video.mjpeg")
    async def video_stream() -> StreamingResponse:
        if not video.has_camera:
            raise HTTPException(status_code=503, detail="camera not available")
        return StreamingResponse(
            mjpeg_generator(video),
            media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
        )

    @app.websocket("/api/control")
    async def control(websocket: WebSocket) -> None:
        await websocket.accept()
        client_id = id(websocket)
        role: str | None = None
        try:
            first = await websocket.receive_json()
            role = await _establish_role(websocket, session, client_id, first)
            if role is None:
                return
            await registry.add(websocket)
            while True:
                try:
                    message = await websocket.receive_json()
                except WebSocketDisconnect:
                    raise
                except (json.JSONDecodeError, TypeError, ValueError, UnicodeDecodeError):
                    # A malformed frame (non-JSON text, an undecodable byte
                    # sequence) from a stale or buggy client is dropped, not
                    # fatal -- only an actual disconnect ends the connection.
                    continue
                if role != "pilot":
                    continue
                if isinstance(message, dict) and "play" in message:
                    # `{"play": ...}` is a one-off gesture-playback request,
                    # independent of the per-100ms control frame (and its
                    # deadman heartbeat) validated below -- see
                    # `_validate_play_message`.
                    gesture = _validate_play_message(message)
                    if gesture is not None:
                        session.play(client_id, gesture)
                    continue
                validated = _validate_pilot_message(message)
                if validated is None:
                    continue
                move, rotate, pitch, yaw = validated
                session.update_pilot_input(client_id, move=move, rotate=rotate, neck_pitch=pitch, neck_yaw=yaw)
        except WebSocketDisconnect:
            pass
        finally:
            await registry.remove(websocket)
            if role == "pilot":
                session.release_pilot(client_id)

    return app


async def _establish_role(
    websocket: WebSocket, session: PilotSession, client_id: int, first_message: dict[str, Any]
) -> str | None:
    """Handle the WebSocket's first message (`{"role": ...}`) and reply with the grant.

    Returns:
        str, optional: `"pilot"` or `"viewer"` on success, `None` if the
            connection was closed for an unrecognized role (the caller must
            not proceed to the read loop in that case).
    """
    requested = first_message.get("role")
    if requested == "pilot":
        if session.acquire_pilot(client_id):
            await websocket.send_json({"granted": True})
            return "pilot"
        await websocket.send_json({"granted": False, "reason": "busy"})
        return "viewer"
    if requested == "viewer":
        await websocket.send_json({"granted": True})
        return "viewer"
    await websocket.close(code=1008)
    return None


async def _broadcast_loop(
    robot: Palmimo, session: PilotSession, robot_loop: RobotLoop, video: VideoStream, registry: _ConnectionRegistry
) -> None:
    """Push the `@2Hz` status payload to every connected client until cancelled."""
    while True:
        await asyncio.sleep(STATUS_BROADCAST_INTERVAL_S)
        status = session.status()
        await registry.broadcast(
            {
                "pilot_present": status.pilot_present,
                "motion": status.motion,
                "loop_fps": robot_loop.loop_fps,
                "camera_ok": video.camera_ok,
                "robot_ok": robot_loop.robot_ok,
                # Whether a servo driver was attached at startup -- `False`
                # means the app is running compute-only (no probe found a
                # bus, or --dry-run), distinct from `robot_ok`, which is
                # about a control-cycle failure on an attached driver.
                "servo_attached": robot.driver is not None,
            }
        )
