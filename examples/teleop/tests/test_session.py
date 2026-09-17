"""Behavior of `palmimo_teleop.session.PilotSession`: slot ownership and the deadman timer.
The clock is injected so the 400ms deadman window never needs a real sleep.
"""

from palmimo_teleop.session import DEADMAN_TIMEOUT_S, GESTURE_PLAY_SECONDS, PilotInput, PilotSession


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _session() -> tuple[PilotSession, FakeClock]:
    clock = FakeClock()
    return PilotSession(now=clock), clock


def _heartbeat(session: PilotSession, client_id: str = "a") -> None:
    """Send one idle control frame -- establishes/refreshes the deadman clock
    without itself steering (and so without cancelling a playing gesture),
    matching the pilot page's routine 100ms frame while nothing is held."""
    session.update_pilot_input(client_id, move=None, rotate=0, neck_pitch=0.0, neck_yaw=0.0)


def _keep_deadman_alive_for(session: PilotSession, clock: FakeClock, seconds: float) -> None:
    """Advance *clock* by *seconds*, sending an idle heartbeat often enough that the
    400ms deadman never lapses -- simulates the pilot's routine 100ms frames
    continuing to arrive while a gesture plays out its window."""
    step = DEADMAN_TIMEOUT_S / 2
    remaining = seconds
    while remaining > 0:
        advance = min(step, remaining)
        clock.advance(advance)
        remaining -= advance
        _heartbeat(session)


def test_acquire_pilot_grants_the_first_claimant() -> None:
    # Without this, no one could ever become pilot.
    session, _ = _session()
    assert session.acquire_pilot("a") is True
    assert session.pilot_present is True


def test_acquire_pilot_is_idempotent_for_the_current_holder() -> None:
    # Without this, a pilot's own reconnect-free re-send of {"role": "pilot"}
    # would spuriously read as "busy".
    session, _ = _session()
    session.acquire_pilot("a")
    assert session.acquire_pilot("a") is True


def test_acquire_pilot_rejects_a_second_claimant() -> None:
    # Without this, two pilots could both drive the robot at once.
    session, _ = _session()
    session.acquire_pilot("a")
    assert session.acquire_pilot("b") is False
    assert session.pilot_present is True


def test_release_pilot_frees_the_slot_for_a_new_claimant() -> None:
    # Without this, a disconnected pilot would permanently lock the slot --
    # nobody could ever drive again.
    session, _ = _session()
    session.acquire_pilot("a")
    session.release_pilot("a")
    assert session.pilot_present is False
    assert session.acquire_pilot("b") is True


def test_release_pilot_by_a_non_holder_is_a_no_op() -> None:
    # Without this, a viewer (or a stale pilot who already lost the slot)
    # could evict the real pilot just by calling release.
    session, _ = _session()
    session.acquire_pilot("a")
    session.release_pilot("b")
    assert session.pilot_present is True


def test_update_pilot_input_by_non_pilot_is_dropped() -> None:
    # Without this, a busy-rejected pilot (now a viewer) could still steer
    # the robot by sending control frames anyway.
    session, _ = _session()
    session.acquire_pilot("a")
    accepted = session.update_pilot_input("b", move="forward", rotate=0, neck_pitch=0.0, neck_yaw=0.0)
    assert accepted is False
    assert session.effective_input().move is None


def test_effective_input_reflects_the_latest_pilot_frame() -> None:
    # Without this, the robot loop would never see what the pilot is pressing.
    session, _ = _session()
    session.acquire_pilot("a")
    session.update_pilot_input("a", move="forward", rotate=0, neck_pitch=0.2, neck_yaw=-0.3)
    assert session.effective_input() == PilotInput(move="forward", rotate=0, neck_pitch=0.2, neck_yaw=-0.3)


def test_effective_input_stops_translation_after_the_deadman_window() -> None:
    # Without this, a pilot whose connection silently died keeps walking the
    # robot forever instead of stopping within 400ms.
    session, clock = _session()
    session.acquire_pilot("a")
    session.update_pilot_input("a", move="forward", rotate=1, neck_pitch=0.5, neck_yaw=0.5)
    clock.advance(DEADMAN_TIMEOUT_S + 0.01)
    effective = session.effective_input()
    assert effective.move is None
    assert effective.rotate == 0


def test_effective_input_recenters_the_neck_after_the_deadman_window() -> None:
    # Without this, a lost connection would leave the neck frozen wherever
    # the pilot last aimed it -- holding an extreme neck position
    # unattended is a known cause of neck-servo overheating on real
    # hardware, unlike un-commanded translation which is simply stopped.
    session, clock = _session()
    session.acquire_pilot("a")
    session.update_pilot_input("a", move="forward", rotate=0, neck_pitch=0.5, neck_yaw=-0.5)
    clock.advance(DEADMAN_TIMEOUT_S + 0.01)
    effective = session.effective_input()
    assert effective.neck_pitch == 0.0
    assert effective.neck_yaw == 0.0


def test_effective_input_with_no_pilot_is_inert() -> None:
    # Without this, the robot loop would have no defined behavior before any
    # pilot ever connects.
    session, _ = _session()
    assert session.effective_input() == PilotInput()


def test_effective_input_stops_gesture_after_the_deadman_window() -> None:
    # Without this, a pilot whose connection silently died could leave dance
    # looping on the robot forever instead of stopping within 400ms.
    session, clock = _session()
    session.acquire_pilot("a")
    _heartbeat(session)
    session.play("a", "dance")
    clock.advance(DEADMAN_TIMEOUT_S + 0.01)
    assert session.effective_input().gesture is None


def test_play_by_the_pilot_is_reflected_in_effective_input() -> None:
    # Without this, tapping a gesture button would have no way to reach the
    # robot loop at all.
    session, _ = _session()
    session.acquire_pilot("a")
    _heartbeat(session)
    assert session.play("a", "wave") is True
    assert session.effective_input().gesture == "wave"


def test_play_by_a_non_pilot_is_dropped() -> None:
    # Without this, a viewer (or a busy-rejected second pilot) could trigger
    # a gesture on someone else's robot.
    session, _ = _session()
    session.acquire_pilot("a")
    _heartbeat(session)
    assert session.play("b", "wave") is False
    assert session.effective_input().gesture is None


def test_play_ends_after_its_window() -> None:
    # Without this, a played gesture (dance loops until deselected) would
    # keep overriding the robot forever instead of the play button being a
    # bounded tap.
    session, clock = _session()
    session.acquire_pilot("a")
    _heartbeat(session)
    session.play("a", "dance")
    _keep_deadman_alive_for(session, clock, GESTURE_PLAY_SECONDS["dance"] + 0.01)
    assert session.effective_input().gesture is None


def test_replaying_the_active_gesture_stops_it_early() -> None:
    # Without this, tapping the same gesture button again mid-playback would
    # either restart it or do nothing, instead of the pilot being able to
    # cut it short.
    session, clock = _session()
    session.acquire_pilot("a")
    _heartbeat(session)
    session.play("a", "wave")
    _keep_deadman_alive_for(session, clock, GESTURE_PLAY_SECONDS["wave"] / 2)
    session.play("a", "wave")
    assert session.effective_input().gesture is None


def test_playing_a_different_gesture_replaces_the_running_one() -> None:
    # Without this, tapping Dance while Wave is still playing would leave
    # Wave running (or do nothing) instead of switching to Dance.
    session, _ = _session()
    session.acquire_pilot("a")
    _heartbeat(session)
    session.play("a", "wave")
    session.play("a", "dance")
    assert session.effective_input().gesture == "dance"


def test_steering_input_cancels_a_playing_gesture() -> None:
    # Without this, pressing a direction while a gesture is playing would
    # either be ignored (gesture always wins in resolve_motion) or blend
    # unpredictably, instead of the pilot's steering intent taking over
    # immediately.
    session, _ = _session()
    session.acquire_pilot("a")
    _heartbeat(session)
    session.play("a", "dance")
    session.update_pilot_input("a", move="forward", rotate=0, neck_pitch=0.0, neck_yaw=0.0)
    effective = session.effective_input()
    assert effective.gesture is None
    assert effective.move == "forward"


def test_idle_input_does_not_cancel_a_playing_gesture() -> None:
    # Without this, the pilot page's routine 100ms heartbeat frame (which
    # carries no move/rotate while nothing is held) would cancel a gesture
    # the instant it started playing.
    session, _ = _session()
    session.acquire_pilot("a")
    _heartbeat(session)
    session.play("a", "dance")
    session.update_pilot_input("a", move=None, rotate=0, neck_pitch=0.0, neck_yaw=0.0)
    assert session.effective_input().gesture == "dance"


def test_status_reports_resolved_motion() -> None:
    # Without this, the @2Hz broadcast would have no way to tell viewers
    # what the robot is actually doing.
    session, _ = _session()
    session.acquire_pilot("a")
    session.update_pilot_input("a", move=None, rotate=1, neck_pitch=0.0, neck_yaw=0.0)
    status = session.status()
    assert status.pilot_present is True
    assert status.motion == "rotate_right"
