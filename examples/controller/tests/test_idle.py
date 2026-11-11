"""Behavior of `palmimo_controller.ai.idle`: small idle behaviours between AI replies, on a fake clock."""

import asyncio
import itertools
import json
import random
from typing import Any

from palmimo_controller.ai.idle import (
    IDLE_AFTER_S,
    IDLE_PAUSE_S,
    STRETCH_MIN_INTERVAL_S,
    Idle,
    IdleJudge,
)
from palmimo_controller.session import AI_START_NOD_SECONDS, PilotInput, PilotSession
from palmimo_controller.telemetry import Telemetry


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


STEP = 0.2


def _live(seed: int = 0) -> tuple[PilotSession, FakeClock, list[dict[str, Any]], IdleJudge]:
    clock = FakeClock()
    session = PilotSession(now=clock)
    session.acquire_pilot("a")
    assert session.begin_ai("a", "default")
    session.ai_started()
    events: list[dict[str, Any]] = []
    judge = IdleJudge(
        session,
        rng=random.Random(seed),
        telemetry=Telemetry(emit=lambda line: events.append(json.loads(line))),
    )
    return session, clock, events, judge


def _run(judge: IdleJudge, clock: FakeClock, seconds: float) -> None:
    for _ in range(round(seconds / STEP)):
        judge.update(clock.now)
        clock.now += STEP


def _idle_doing_something(session: PilotSession) -> bool:
    return session.effective_input() != PilotInput() or session.face_active


def test_idle_waits_for_quiet_time_after_the_ai_moves() -> None:
    _, clock, events, judge = _live()
    # The go-live nod counts as the AI moving, so the quiet time starts after it.
    _run(judge, clock, AI_START_NOD_SECONDS + IDLE_AFTER_S - STEP)
    assert events == []
    _run(judge, clock, 2 * STEP)
    assert [event["type"] for event in events] == ["idle"]


def test_idle_pauses_between_behaviours() -> None:
    _, clock, events, judge = _live()
    _run(judge, clock, AI_START_NOD_SECONDS + IDLE_AFTER_S + STEP)
    assert len(events) == 1
    # No behaviour runs longer than 3 s, so the next one comes within that plus the longest pause.
    _run(judge, clock, IDLE_PAUSE_S[0])
    assert len(events) == 1
    _run(judge, clock, 3.0 + IDLE_PAUSE_S[1])
    assert len(events) >= 2


def test_idle_yields_at_once_to_an_ai_intent() -> None:
    for seed in range(10):
        session, clock, _, judge = _live(seed)
        _run(judge, clock, AI_START_NOD_SECONDS + IDLE_AFTER_S + STEP)
        assert _idle_doing_something(session)
        session.set_ai_intent(PilotInput(move="forward"), 2.0)
        assert session.effective_input() == PilotInput(move="forward")
        assert not session.idle_active
        # An idle face ends too; only an AI-chosen face outlives a motion intent.
        assert not session.face_active


def test_idle_yields_at_once_to_a_glance() -> None:
    for seed in range(10):
        session, clock, _, judge = _live(seed)
        _run(judge, clock, AI_START_NOD_SECONDS + IDLE_AFTER_S + STEP)
        session.set_gaze(0.1, -0.1)
        assert session.effective_input() == PilotInput(neck_pitch=0.1, neck_yaw=-0.1)
        assert not session.idle_active and not session.face_active


def test_idle_does_not_start_during_a_glance() -> None:
    session, clock, events, judge = _live()
    clock.now = AI_START_NOD_SECONDS
    session.set_gaze(0.2, 0.2)
    _run(judge, clock, IDLE_AFTER_S * 3)
    assert events == []


def test_idle_never_walks_turns_or_moves_the_neck() -> None:
    # Idle movement read as restless on the robot: only faces and a rare stretch remain.
    session, clock, events, judge = _live(seed=42)
    clock.now = AI_START_NOD_SECONDS
    for _ in range(round(1200 / STEP)):
        judge.update(clock.now)
        effective = session.effective_input()
        assert effective.move is None and effective.rotate == 0
        assert effective.neck_pitch == 0 and effective.neck_yaw == 0
        clock.now += STEP
    assert {event["behaviour"] for event in events if event["type"] == "idle"} == {"face", "stretch"}


def test_idle_stretches_at_most_once_per_interval() -> None:
    for seed in range(10):
        _, clock, events, judge = _live(seed)
        clock.now = AI_START_NOD_SECONDS
        stretches: list[float] = []
        for _ in range(round(1800 / STEP)):
            seen = len(events)
            judge.update(clock.now)
            if any(event.get("behaviour") == "stretch" and event["type"] == "idle" for event in events[seen:]):
                stretches.append(clock.now)
            clock.now += STEP
        assert len(stretches) <= 1800 / STRETCH_MIN_INTERVAL_S + 1
        assert all(later - earlier >= STRETCH_MIN_INTERVAL_S for earlier, later in itertools.pairwise(stretches))


def test_idle_never_overrides_an_ai_face() -> None:
    session, clock, _, judge = _live()
    clock.now = AI_START_NOD_SECONDS
    session.set_ai_face("LOVE", 5.0)
    for _ in range(round(5.0 / STEP) - 1):
        judge.update(clock.now)
        assert session.face_cue().expression == "LOVE"
        clock.now += STEP


def test_idle_does_nothing_outside_ai_mode() -> None:
    session, clock, events, judge = _live()
    session.end_ai()
    _run(judge, clock, IDLE_AFTER_S * 3)
    assert events == []
    assert session.effective_input() == PilotInput()


def test_set_idle_refuses_walking_and_turning() -> None:
    session, clock, _, _ = _live()
    clock.now = AI_START_NOD_SECONDS
    assert session.set_idle(PilotInput(move="forward"), 2.0) is False
    assert session.set_idle(PilotInput(rotate=1), 2.0) is False
    assert session.effective_input() == PilotInput()


async def test_idle_runs_and_stops_with_the_session_task() -> None:
    session, clock, _, _ = _live()
    clock.now = AI_START_NOD_SECONDS  # past the go-live nod, so only idle can move the robot
    idle = Idle(session, rng=random.Random(1), period=0.001, now=clock)
    task = asyncio.create_task(idle.run())
    async with asyncio.timeout(2.0):
        while not _idle_doing_something(session):
            clock.now += 0.5
            await asyncio.sleep(0.002)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    # Cancelling (the AI session ending) leaves the robot still and the face neutral.
    assert session.effective_input() == PilotInput()
    assert not session.face_active


def test_idle_event_carries_its_parameters() -> None:
    _, clock, events, judge = _live(seed=0)
    clock.now = AI_START_NOD_SECONDS
    _run(judge, clock, 120)
    faces = [event for event in events if event.get("behaviour") == "face" and event["type"] == "idle"]
    assert faces and all(event["expression"] in ("SMILE", "THINKING") for event in faces)
    assert all("duration_s" in event for event in events if event["type"] == "idle")


def test_idle_cut_short_by_an_ai_intent_is_logged_with_the_reason() -> None:
    for seed in range(20):
        session, clock, events, judge = _live(seed)
        _run(judge, clock, AI_START_NOD_SECONDS + IDLE_AFTER_S + STEP)
        session.set_ai_intent(PilotInput(gesture="nod"), 1.0)
        judge.update(clock.now)
        ends = [event for event in events if event["type"] == "idle_end"]
        assert len(ends) == 1 and ends[0]["reason"] == "ai_intent"
