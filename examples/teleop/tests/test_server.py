"""Behavior of the FastAPI app assembled by `palmimo_teleop.server.create_app`, against a
`--dry-run --no-camera` `Palmimo` and the real `PilotSession`/`RobotLoop`/`VideoStream` --
no real servo bus, no real camera, no real time."""

import time
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from palmimo_sdk import Palmimo, ServoDriver
from palmimo_teleop.robot_loop import RobotLoop
from palmimo_teleop.server import _validate_pilot_message, _validate_play_message, create_app
from palmimo_teleop.session import PilotSession
from palmimo_teleop.video import VideoStream


class _FakeServoDriver(ServoDriver):
    """Minimal `ServoDriver` double: just enough for `Palmimo.connect()`/`disconnect()`
    to succeed with no real bus, so a broadcast test can attach a driver."""

    def __init__(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def _write_positions(self, positions: dict[str, int]) -> None:
        pass


@pytest.fixture
def client() -> Iterator[TestClient]:
    robot = Palmimo(driver=None)  # dry-run equivalent: no connectable resource
    session = PilotSession()
    robot_loop = RobotLoop(robot, session, fps=30)
    video = VideoStream(None)  # --no-camera equivalent
    app = create_app(robot, session, robot_loop, video)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def client_and_session() -> Iterator[tuple[TestClient, PilotSession]]:
    # A separate fixture (rather than exposing `session` off `client`) for the
    # few tests that need to assert on session state directly instead of via
    # the @2Hz status broadcast -- that broadcast's own cadence (500ms) is
    # longer than the real (non-fake-clock) `PilotSession`'s 400ms deadman
    # window here, so waiting for it to reflect one already-sent frame is racy.
    robot = Palmimo(driver=None)
    session = PilotSession()
    robot_loop = RobotLoop(robot, session, fps=30)
    video = VideoStream(None)
    app = create_app(robot, session, robot_loop, video)
    with TestClient(app) as test_client:
        yield test_client, session


def test_viewer_page_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200


def test_pilot_page_is_served(client: TestClient) -> None:
    response = client.get("/pilot")
    assert response.status_code == 200


def test_video_endpoint_503s_without_a_camera(client: TestClient) -> None:
    # Without this, a --no-camera deployment would either hang or 200 with
    # no data instead of telling the frontend plainly that video is off.
    response = client.get("/video.mjpeg")
    assert response.status_code == 503


def test_first_pilot_is_granted_the_control_slot(client: TestClient) -> None:
    # Without this, nobody could ever become pilot over the WebSocket.
    with client.websocket_connect("/api/control") as ws:
        ws.send_json({"role": "pilot"})
        assert ws.receive_json() == {"granted": True}


def test_second_pilot_is_rejected_as_busy(client: TestClient) -> None:
    # Without this, two browser tabs could both drive the robot at once.
    with client.websocket_connect("/api/control") as first:
        first.send_json({"role": "pilot"})
        first.receive_json()
        with client.websocket_connect("/api/control") as second:
            second.send_json({"role": "pilot"})
            assert second.receive_json() == {"granted": False, "reason": "busy"}


def test_pilot_slot_is_freed_on_disconnect(client: TestClient) -> None:
    # Without this, a pilot's dropped connection would permanently lock
    # everyone else out with no automatic recovery.
    with client.websocket_connect("/api/control") as first:
        first.send_json({"role": "pilot"})
        first.receive_json()

    with client.websocket_connect("/api/control") as second:
        second.send_json({"role": "pilot"})
        assert second.receive_json() == {"granted": True}


def test_status_broadcast_reports_servo_attached_false_without_a_driver(client: TestClient) -> None:
    # Without this, a compute-only run (no probe found a bus, or --dry-run)
    # could broadcast no signal -- or a stale/incorrect one -- of that fact,
    # leaving the pilot page's compute-only banner with nothing to key off.
    with client.websocket_connect("/api/control") as ws:
        ws.send_json({"role": "viewer"})
        ws.receive_json()
        assert ws.receive_json()["servo_attached"] is False


def test_status_broadcast_reports_servo_attached_true_with_a_driver() -> None:
    # Without this, an attached driver could be reported the same as
    # compute-only, hiding the distinction the whole feature exists to draw.
    robot = Palmimo(driver=_FakeServoDriver())
    session = PilotSession()
    robot_loop = RobotLoop(robot, session, fps=30)
    video = VideoStream(None)
    app = create_app(robot, session, robot_loop, video)
    with TestClient(app) as test_client, test_client.websocket_connect("/api/control") as ws:
        ws.send_json({"role": "viewer"})
        ws.receive_json()
        assert ws.receive_json()["servo_attached"] is True


def test_viewer_role_is_always_granted(client: TestClient) -> None:
    # Without this, a viewer-only connection (the / page) would have no way
    # to receive status updates.
    with client.websocket_connect("/api/control") as ws:
        ws.send_json({"role": "viewer"})
        assert ws.receive_json() == {"granted": True}


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 2.0) -> bool:
    """Poll a zero-arg callable until it returns truthy or *timeout* elapses.

    Only used for state that a background thread/task updates asynchronously
    (here: `PilotSession`'s real, non-fake-clock deadman timer) -- there is no
    signal to await instead.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_pilot_connection_survives_malformed_frames_and_applies_the_next_valid_one(
    client_and_session: tuple[TestClient, PilotSession],
) -> None:
    # Without this, a malformed pilot frame -- non-JSON text (JSONDecodeError
    # from receive_json), a bare JSON array, or `{"neck": "evil"}` -- could
    # kill the whole control connection instead of just being dropped.
    client, session = client_and_session
    with client.websocket_connect("/api/control") as ws:
        ws.send_json({"role": "pilot"})
        ws.receive_json()
        ws.send_text("not json")
        ws.send_json([1, 2, 3])
        ws.send_json({"neck": "evil"})
        ws.send_json({"move": "backflip", "rotate": 0, "neck": {"pitch": 0.0, "yaw": 0.0}, "seq": 1})
        ws.send_json({"move": "forward", "rotate": 0, "neck": {"pitch": 0.0, "yaw": 0.0}, "seq": 2})
        assert _wait_for(lambda: session.effective_input().move == "forward")


def test_resume_message_is_ignored_as_an_unrecognized_frame(
    client_and_session: tuple[TestClient, PilotSession],
) -> None:
    # Without this, a stale client still sending the retired `{"resume":
    # true}` frame could be silently mistaken for a valid control frame
    # instead of being dropped like any other frame `_validate_pilot_message`
    # rejects.
    client, session = client_and_session
    with client.websocket_connect("/api/control") as ws:
        ws.send_json({"role": "pilot"})
        ws.receive_json()
        ws.send_json({"resume": True})
        ws.send_json({"move": "forward", "rotate": 0, "neck": {"pitch": 0.0, "yaw": 0.0}, "seq": 1})
        assert _wait_for(lambda: session.effective_input().move == "forward")


def test_validate_pilot_message_rejects_a_non_dict_message() -> None:
    # Without this, a bare JSON array (`[1,2,3]`) would hit `.get` on a list
    # and raise `AttributeError`, crashing the WebSocket handler.
    assert _validate_pilot_message([1, 2, 3]) is None


def test_validate_pilot_message_rejects_a_non_dict_neck() -> None:
    # Without this, `{"neck": "evil"}` would hit `.get` on a string the same way.
    assert _validate_pilot_message({"move": None, "rotate": 0, "neck": "evil"}) is None


def test_validate_pilot_message_rejects_bool_rotate() -> None:
    # Without this, `rotate: true` would be silently accepted as `rotate: 1`
    # (Python's `True == 1`), letting a client rotate the robot without ever
    # sending an actual integer through `ALLOWED_ROTATES`.
    assert _validate_pilot_message({"move": None, "rotate": True, "neck": {}}) is None


@pytest.mark.parametrize("bad_move", ["backflip", "", "FORWARD", "sit"])
def test_validate_pilot_message_rejects_unknown_move(bad_move: str) -> None:
    # Without this, a compromised or buggy client could invoke a gait name
    # `control.resolve_motion` never validated, bypassing `ALLOWED_MOVES`.
    assert _validate_pilot_message({"move": bad_move, "rotate": 0, "neck": {}}) is None


@pytest.mark.parametrize("bad_rotate", [2, -2, 0.5, "left"])
def test_validate_pilot_message_rejects_out_of_set_rotate(bad_rotate: object) -> None:
    # Without this, a client could send an arbitrary rotate value that
    # `control.resolve_motion` was never designed to handle.
    assert _validate_pilot_message({"move": None, "rotate": bad_rotate, "neck": {}}) is None


def test_validate_pilot_message_clamps_neck_instead_of_rejecting() -> None:
    # Without this, an out-of-range neck value (an eager client, a stick
    # library bug) would either be rejected outright or reach `Palmimo.look`
    # unclamped -- no neck value is itself unsafe, unlike an unknown gait name.
    validated = _validate_pilot_message({"move": None, "rotate": 0, "neck": {"pitch": 5.0, "yaw": -5.0}})
    assert validated == (None, 0, 1.0, -1.0)


def test_validate_pilot_message_accepts_a_well_formed_frame() -> None:
    # Without this, valid pilot input itself could regress silently under
    # the same validation path that guards against bad input.
    validated = _validate_pilot_message({"move": "forward", "rotate": 1, "neck": {"pitch": 0.2, "yaw": -0.1}})
    assert validated == ("forward", 1, 0.2, -0.1)


def test_validate_play_message_accepts_an_allowed_gesture() -> None:
    assert _validate_play_message({"play": "wave"}) == "wave"


@pytest.mark.parametrize("bad_play", ["bogus", "", 1, None])
def test_validate_play_message_rejects_anything_outside_allowed_gestures(bad_play: object) -> None:
    # Without this, a client could invoke a gesture name `control.resolve_motion`
    # never validated, bypassing `ALLOWED_GESTURES`.
    assert _validate_play_message({"play": bad_play}) is None


def test_play_message_from_the_pilot_starts_the_gesture(
    client_and_session: tuple[TestClient, PilotSession],
) -> None:
    # Without this, tapping a gesture button on the pilot page would never
    # reach the robot loop.
    client, session = client_and_session
    with client.websocket_connect("/api/control") as ws:
        ws.send_json({"role": "pilot"})
        ws.receive_json()
        # A control frame first, so the 400ms deadman is already satisfied
        # by the time `play` lands -- matches the pilot page, which starts
        # its 100ms heartbeat as soon as the socket opens.
        ws.send_json({"move": None, "rotate": 0, "neck": {"pitch": 0.0, "yaw": 0.0}, "seq": 1})
        ws.send_json({"play": "dance"})
        assert _wait_for(lambda: session.effective_input().gesture == "dance")


def test_play_message_from_a_viewer_is_dropped(
    client_and_session: tuple[TestClient, PilotSession],
) -> None:
    # Without this, a spectator on `/` could trigger a gesture on the robot
    # someone else is piloting.
    client, session = client_and_session
    with client.websocket_connect("/api/control") as pilot, client.websocket_connect("/api/control") as viewer:
        pilot.send_json({"role": "pilot"})
        pilot.receive_json()
        viewer.send_json({"role": "viewer"})
        viewer.receive_json()
        viewer.send_json({"play": "dance"})
        # A frame from the real pilot gives `_wait_for` something to poll
        # for instead of a fixed sleep; the viewer's play is rejected by
        # `PilotSession.play`'s own pilot-id check regardless of ordering
        # between the two connections.
        pilot.send_json({"move": "forward", "rotate": 0, "neck": {"pitch": 0.0, "yaw": 0.0}, "seq": 1})
        assert _wait_for(lambda: session.effective_input().move == "forward")
        assert session.effective_input().gesture is None
