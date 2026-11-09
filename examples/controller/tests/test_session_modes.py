"""Behavior of `PilotSession`'s teleop/AI mode switching. The clock is injected."""

from palmimo_controller.session import (
    AI_INTENT_MAX_SECONDS,
    AI_START_NOD_SECONDS,
    CUE_FACE_HOLD_MS,
    FAILED_FACE,
    IDLE_FACE_CUE,
    LIVE_FACE,
    MODE_AI,
    MODE_AI_STARTING,
    MODE_TELEOP,
    STARTING_FACE_SECONDS,
    STARTING_FACES,
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
    session, clock = _pilot_session()
    _live_ai(session)
    clock.now = AI_START_NOD_SECONDS
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


def test_ai_going_live_nods_once() -> None:
    session, clock = _pilot_session()
    _live_ai(session)
    assert session.effective_input() == PilotInput(gesture="nod")
    clock.now = AI_START_NOD_SECONDS
    assert session.effective_input() == PilotInput()


def test_face_cue_cycles_while_ai_starts() -> None:
    session, clock = _pilot_session()
    session.begin_ai("a", "default")
    seen = []
    for step in range(len(STARTING_FACES) + 1):
        # Mid-step, so float rounding at a step boundary cannot pick the previous face.
        clock.now = (step + 0.5) * STARTING_FACE_SECONDS
        cue = session.face_cue()
        seen.append((cue.key, cue.expression))
    assert [expression for _, expression in seen] == [*STARTING_FACES, STARTING_FACES[0]]
    assert len({key for key, _ in seen}) == len(seen)


def test_face_cue_is_happy_when_ai_is_live() -> None:
    session, _ = _pilot_session()
    _live_ai(session)
    cue = session.face_cue()
    assert (cue.expression, cue.hold_ms) == (LIVE_FACE, CUE_FACE_HOLD_MS)


def test_face_cue_is_sad_after_a_failed_start_and_differs_per_failure() -> None:
    session, clock = _pilot_session()
    session.begin_ai("a", "default")
    session.end_ai("network")
    first = session.face_cue()
    assert (first.expression, first.hold_ms) == (FAILED_FACE, CUE_FACE_HOLD_MS)
    clock.now = 1.0
    session.begin_ai("a", "default")
    session.end_ai("network")
    assert session.face_cue().key != first.key


def test_face_cue_is_idle_in_plain_teleop() -> None:
    session, _ = _pilot_session()
    assert session.face_cue() == IDLE_FACE_CUE
    _live_ai(session)
    session.end_ai()
    assert session.face_cue() == IDLE_FACE_CUE


def test_gaze_points_the_neck_between_ai_intents() -> None:
    session, clock = _pilot_session()
    _live_ai(session)
    clock.now = AI_START_NOD_SECONDS
    assert session.set_gaze(0.2, -0.4)
    assert session.effective_input() == PilotInput(neck_pitch=0.2, neck_yaw=-0.4)
    session.clear_gaze()
    assert session.effective_input() == PilotInput()


def test_ai_intent_wins_over_a_gaze() -> None:
    session, clock = _pilot_session()
    _live_ai(session)
    clock.now = AI_START_NOD_SECONDS
    session.set_gaze(0.2, -0.4)
    session.set_ai_intent(PilotInput(move="forward"), 1.0)
    assert session.ai_busy
    assert session.effective_input() == PilotInput(move="forward")


def test_gaze_is_ignored_outside_a_live_ai_mode() -> None:
    session, _ = _pilot_session()
    assert session.set_gaze(0.2, 0.2) is False
    session.begin_ai("a", "default")
    assert session.set_gaze(0.2, 0.2) is False
    assert session.effective_input() == PilotInput()


def test_ai_mode_drives_at_the_default_speed_whatever_the_pilot_picked() -> None:
    # Without this, a fast speed picked in teleop would carry into the
    # unattended AI's walking.
    session, _ = _pilot_session()
    session.update_pilot_input("a", move=None, rotate=0, neck_pitch=0.0, neck_yaw=0.0)
    session.set_speed("a", 2.5)
    assert session.begin_ai("a", "default")
    assert session.effective_input().speed == 1.0
    session.ai_started()
    session.set_ai_intent(PilotInput(move="forward"), 3.0)
    assert session.effective_input().speed == 1.0
    assert session.status().speed == 1.0


def test_returning_to_teleop_starts_at_the_default_speed() -> None:
    # Without this, the pilot would come back from AI mode to a fast walk
    # picked before the switch, with no button tap to remind them.
    session, _ = _pilot_session()
    session.set_speed("a", 2.5)
    _live_ai(session)
    session.end_ai()
    session.update_pilot_input("a", move=None, rotate=0, neck_pitch=0.0, neck_yaw=0.0)
    assert session.effective_input().speed == 1.0


def test_set_speed_is_dropped_outside_teleop() -> None:
    # Without this, a speed tap during an AI mode would be stored and come
    # back as a fast walk when the pilot returns to teleop.
    session, _ = _pilot_session()
    _live_ai(session)
    assert session.set_speed("a", 2.5) is False
    session.end_ai()
    session.update_pilot_input("a", move=None, rotate=0, neck_pitch=0.0, neck_yaw=0.0)
    assert session.effective_input().speed == 1.0
