"""Behavior of `PilotSession`'s teleop/AI mode switching. The clock is injected."""

from palmimo_controller.session import (
    AI_INTENT_MAX_SECONDS,
    MODE_AI,
    MODE_AI_STARTING,
    MODE_TELEOP,
    PilotInput,
    PilotSession,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _pilot_session() -> tuple[PilotSession, FakeClock]:
    clock = FakeClock()
    session = PilotSession(now=clock)
    session.acquire_pilot("a")
    return session, clock


def _live_ai(session: PilotSession) -> None:
    assert session.begin_ai("a", "default")
    session.ai_started()


def test_begin_ai_enters_starting_for_the_pilot() -> None:
    session, _ = _pilot_session()
    assert session.begin_ai("a", "default") is True
    assert session.mode == MODE_AI_STARTING
    assert session.ai_mode == "default"


def test_begin_ai_ignores_a_second_tap_while_switching() -> None:
    # Without this, a double tap could start two AI sessions.
    session, _ = _pilot_session()
    session.begin_ai("a", "default")
    assert session.begin_ai("a", "kansai") is False
    assert session.ai_mode == "default"


def test_begin_ai_rejects_a_non_pilot() -> None:
    session, _ = _pilot_session()
    assert session.begin_ai("b", "default") is False
    assert session.mode == MODE_TELEOP


def test_effective_input_holds_still_while_ai_starts() -> None:
    session, _ = _pilot_session()
    session.update_pilot_input("a", move="forward", rotate=0, neck_pitch=0.0, neck_yaw=0.0)
    session.begin_ai("a", "default")
    assert session.effective_input() == PilotInput()


def test_effective_input_ignores_pilot_input_in_ai_mode() -> None:
    session, _ = _pilot_session()
    _live_ai(session)
    session.update_pilot_input("a", move="forward", rotate=0, neck_pitch=0.0, neck_yaw=0.0)
    assert session.effective_input() == PilotInput()


def test_ai_intent_drives_the_robot_until_it_expires() -> None:
    session, clock = _pilot_session()
    _live_ai(session)
    assert session.set_ai_intent(PilotInput(move="forward"), 2.0)
    assert session.effective_input().move == "forward"
    clock.now = 2.0
    assert session.effective_input() == PilotInput()


def test_ai_intent_is_capped() -> None:
    # Without this, a model asking to walk for a minute would walk for a minute.
    session, clock = _pilot_session()
    _live_ai(session)
    session.set_ai_intent(PilotInput(move="forward"), 60.0)
    clock.now = AI_INTENT_MAX_SECONDS
    assert session.effective_input() == PilotInput()


def test_ai_intent_is_ignored_outside_ai_mode() -> None:
    session, _ = _pilot_session()
    assert session.set_ai_intent(PilotInput(move="forward"), 2.0) is False
    session.begin_ai("a", "default")
    assert session.set_ai_intent(PilotInput(move="forward"), 2.0) is False


def test_end_ai_returns_to_teleop_and_drops_the_intent() -> None:
    session, _ = _pilot_session()
    _live_ai(session)
    session.set_ai_intent(PilotInput(move="forward"), 2.0)
    session.end_ai("network down")
    status = session.status()
    assert (status.mode, status.ai_mode, status.notice, status.motion) == (MODE_TELEOP, None, "network down", None)


def test_teleop_input_works_again_after_ai() -> None:
    session, _ = _pilot_session()
    _live_ai(session)
    session.end_ai()
    session.update_pilot_input("a", move="backward", rotate=0, neck_pitch=0.0, neck_yaw=0.0)
    assert session.effective_input().move == "backward"


def test_status_reports_the_live_ai_mode() -> None:
    session, _ = _pilot_session()
    _live_ai(session)
    status = session.status()
    assert (status.mode, status.ai_mode) == (MODE_AI, "default")
