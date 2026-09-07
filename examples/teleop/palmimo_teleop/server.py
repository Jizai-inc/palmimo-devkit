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
from urllib.parse import urlsplit

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

#: Cap on one client's `send_json` during a status broadcast -- a stalled
#: peer (a dead TCP path that has not yet surfaced as a close) must not hold
#: up delivery to every other connected client.
BROADCAST_SEND_TIMEOUT_S: float = 1.0


class _ConnectionRegistry:
    """Every open `/api/control` WebSocket, for the periodic status broadcast.

    A plain dict keyed by the socket object rather than by role -- both
    pilot and viewers receive the same broadcast, so there is nothing role
    differentiates here.
    """

    def __init__(self, *, send_timeout_s: float = BROADCAST_SEND_TIMEOUT_S) -> None:
        self._sockets: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._send_timeout_s = send_timeout_s

    async def add(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._sockets.add(websocket)

    async def remove(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._sockets.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """Send *payload* to every registered socket, independently of one another.

        Each send is wrapped in its own timeout and exception guard
        (`_send_one`, which never raises) and the whole batch runs
        concurrently via `asyncio.gather` -- one client that raises, or
        whose `send_json` never completes (a stalled peer), must not delay
        or block delivery to the rest.
        """
        async with self._lock:
            targets = list(self._sockets)
        if not targets:
            return
        await asyncio.gather(*(self._send_one(websocket, payload) for websocket in targets))

    async def _send_one(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(websocket.send_json(payload), timeout=self._send_timeout_s)


def _neck_axis(value: object) -> float | None:
    """Validate and clamp one `neck.pitch`/`neck.yaw` value from an untrusted pilot frame.

    Returns:
        float, optional: the clamped axis value, or `None` (frame dropped)
            if *value* is not a plain `int`/`float` -- `bool` is excluded
            despite being an `int` subclass, matching `rotate`'s treatment --
            or is numeric but too large for `float()` to represent
            (`OverflowError`, distinct from the `ValueError`/`TypeError` a
            non-numeric value would raise). Both must be rejected before
            `clamp_neck`'s `math.isfinite` guard ever runs.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return clamp_neck(float(value))
    except OverflowError:
        return None


def _validate_pilot_message(message: object) -> tuple[str | None, int, float, float] | None:
    """Validate one pilot control frame.

    *message* is untyped `object`, not `dict[str, Any]`: it is whatever
    `json.loads` produced from an untrusted client, which for a bare JSON
    array or string is not a dict at all -- calling `.get` on it would raise
    `AttributeError` and kill the WebSocket handler, so every shape
    (including a `move`/`rotate` sent as a list, or a `neck` value large
    enough to overflow `float()`) is checked before use rather than assumed.

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
    if move is not None and (not isinstance(move, str) or move not in ALLOWED_MOVES):
        return None
    rotate = message.get("rotate", 0)
    # `bool` is a subclass of `int` in Python, so `True in {-1, 0, 1}` is
    # `True` (`True == 1`) -- rejected explicitly rather than silently
    # accepted as rotate=1. `isinstance` is checked before the `in` test:
    # an unhashable `rotate` (e.g. a list) would otherwise raise `TypeError`
    # from the frozenset membership check.
    if isinstance(rotate, bool) or not isinstance(rotate, int) or rotate not in ALLOWED_ROTATES:
        return None
    neck = message.get("neck")
    if neck is None:
        neck = {}
    if not isinstance(neck, dict):
        return None
    pitch = _neck_axis(neck.get("pitch", 0.0))
    yaw = _neck_axis(neck.get("yaw", 0.0))
    if pitch is None or yaw is None:
        return None
    return move, rotate, pitch, yaw


def _validate_play_message(message: dict[str, Any]) -> str | None:
    """Validate a `{"play": ...}` gesture-playback message.

    Returns:
        str, optional: the gesture name if it is in `ALLOWED_GESTURES`, else
            `None` -- the caller drops the message entirely, same as an
            unrecognized `move`. `isinstance` is checked before the `in`
            test: an unhashable `play` value (e.g. a list) would otherwise
            raise `TypeError` from the frozenset membership check.
    """
    name = message.get("play")
    if not isinstance(name, str) or name not in ALLOWED_GESTURES:
        return None
    return name


def _resolve_role(session: PilotSession, client_id: int, first_message: object) -> tuple[str | None, dict[str, Any]]:
    """Decide the role for a WebSocket's first message, without performing any I/O.

    Split out from the connection handler so the pilot slot is claimed (via
    `session.acquire_pilot`) and the resulting role is known to the caller
    *before* anything is awaited -- if the caller's subsequent
    `send_json(payload)` then fails (a closed peer surfacing as something
    other than `WebSocketDisconnect`, e.g. `RuntimeError`), the role is
    already committed and the caller's `finally` still releases the slot.
    Resolving the role by awaiting `send_json` itself, as a previous version
    did, left the slot permanently held whenever that send failed.

    Returns:
        tuple: `(role, payload)`. `role` is `"pilot"` or `"viewer"` when the
            caller should proceed to the read loop, or `None` when the
            first message was not a dict or requested an unrecognized role
            -- the caller must close the connection with 1008 and never
            send *payload* (which is `{}` in that case).
    """
    if not isinstance(first_message, dict):
        return None, {}
    requested = first_message.get("role")
    if requested == "pilot":
        if session.acquire_pilot(client_id):
            return "pilot", {"granted": True}
        return "viewer", {"granted": False, "reason": "busy"}
    if requested == "viewer":
        return "viewer", {"granted": True}
    return None, {}


def _origin_is_allowed(websocket: WebSocket) -> bool:
    """Whether this WebSocket's `Origin` header, if present, matches the request's own `Host`.

    WebSocket connections are not subject to CORS, so without this check any
    page reachable on the LAN could open `ws://<robot>/api/control` and
    drive the robot. A missing `Origin` header -- any non-browser client,
    including this project's own tests -- is let through unchecked; only a
    mismatched `Origin`, which only a browser navigating from a different
    site can send, is rejected.
    """
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc == websocket.headers.get("host", "")


async def _control_connection(
    websocket: WebSocket, session: PilotSession, registry: _ConnectionRegistry, client_id: int
) -> None:
    """Run one accepted `/api/control` WebSocket's role handshake and read loop.

    Extracted from the route so it can be exercised directly against a fake
    `WebSocket` in tests, without going through a real ASGI handshake.
    """
    role: str | None = None
    try:
        first = await websocket.receive_json()
        role, payload = _resolve_role(session, client_id, first)
        if role is None:
            await websocket.close(code=1008)
            return
        await websocket.send_json(payload)
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
        # Runs for every exit path except a `WebSocketDisconnect` re-raised
        # above having already been caught -- including any other exception
        # propagating out of this function -- so the pilot slot and
        # registry membership are never left dangling on an unexpected
        # error from `send_json`/`receive_json`.
        await registry.remove(websocket)
        if role == "pilot":
            session.release_pilot(client_id)


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
            try:
                await broadcast_task
            except asyncio.CancelledError:
                pass
            except Exception:
                # However the broadcast task ended, robot/video teardown
                # below must still run -- it is the only path that eases
                # the physical robot to a stop and releases the camera.
                _LOG.exception("status broadcast task ended with an unexpected error")
            finally:
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
        if not _origin_is_allowed(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await _control_connection(websocket, session, registry, id(websocket))

    return app


async def _broadcast_loop(
    robot: Palmimo, session: PilotSession, robot_loop: RobotLoop, video: VideoStream, registry: _ConnectionRegistry
) -> None:
    """Push the `@2Hz` status payload to every connected client until cancelled.

    Each iteration is guarded so one failure (e.g. `session.status()` or a
    broadcast raising) is logged and skipped rather than ending the task --
    a task death here would silently stop every client's status updates for
    the rest of the process, with no automatic recovery.
    """
    while True:
        await asyncio.sleep(STATUS_BROADCAST_INTERVAL_S)
        try:
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
        except Exception:
            _LOG.exception("status broadcast failed")
