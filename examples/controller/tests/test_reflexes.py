"""Behavior of `palmimo_controller.ai.reflexes`: wave-back and face glances, judged on a fake clock."""

import asyncio
import json
from typing import Any

from palmimo_controller.ai.reflexes import (
    GLANCE_COOLDOWN_S,
    GLANCE_FACE_HITS,
    GLANCE_LOST_GRACE_S,
    GLANCE_SECONDS,
    WAVE_COOLDOWN_S,
    Detectors,
    Reflexes,
    ReflexJudge,
    Sighting,
)
from palmimo_controller.session import AI_START_NOD_SECONDS, PilotInput, PilotSession
from palmimo_controller.telemetry import Telemetry


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


FACE_RIGHT = (0.8, 0.5)
STEP = 0.1


def _live() -> tuple[PilotSession, FakeClock, list[dict[str, Any]], ReflexJudge]:
    clock = FakeClock()
    session = PilotSession(now=clock)
    session.acquire_pilot("a")
    assert session.begin_ai("a", "default")
    session.ai_started()
    clock.now = AI_START_NOD_SECONDS  # let the go-live nod finish
    events: list[dict[str, Any]] = []
    judge = ReflexJudge(session, telemetry=Telemetry(emit=lambda line: events.append(json.loads(line))))
    return session, clock, events, judge


def _see(judge: ReflexJudge, clock: FakeClock, sighting: Sighting, frames: int = 1) -> None:
    for _ in range(frames):
        judge.update(sighting, clock.now)
        clock.now += STEP


def test_wave_is_waved_back() -> None:
    session, clock, events, judge = _live()
    _see(judge, clock, Sighting(waved=True))
    assert session.effective_input().gesture == "wave"
    assert events[-1]["type"] == "reflex" and events[-1]["reflex"] == "wave"


def test_wave_back_waits_out_its_cooldown() -> None:
    session, clock, _, judge = _live()
    _see(judge, clock, Sighting(waved=True))
    session.set_ai_intent(PilotInput(), 0.0)  # the wave finished
    _see(judge, clock, Sighting(waved=True))
    assert session.effective_input() == PilotInput()
    clock.now += WAVE_COOLDOWN_S
    _see(judge, clock, Sighting(waved=True))
    assert session.effective_input().gesture == "wave"


def test_wave_is_skipped_while_the_ai_is_moving() -> None:
    # A reflex never overrides what the model asked for.
    session, clock, _, judge = _live()
    session.set_ai_intent(PilotInput(move="forward"), 3.0)
    _see(judge, clock, Sighting(waved=True))
    assert session.effective_input().move == "forward"


def test_glance_starts_after_a_few_face_frames_and_turns_toward_it() -> None:
    session, clock, events, judge = _live()
    _see(judge, clock, Sighting(face=FACE_RIGHT), frames=GLANCE_FACE_HITS - 1)
    assert not judge.glancing
    _see(judge, clock, Sighting(face=FACE_RIGHT))
    assert judge.glancing
    assert session.effective_input().neck_yaw > 0
    assert events[-1]["reflex"] == "glance"


def test_glance_recenters_after_its_time_is_up() -> None:
    session, clock, _, judge = _live()
    _see(judge, clock, Sighting(face=FACE_RIGHT), frames=GLANCE_FACE_HITS)
    clock.now += GLANCE_SECONDS
    _see(judge, clock, Sighting(face=FACE_RIGHT))
    assert not judge.glancing
    assert session.effective_input() == PilotInput()


def test_glance_ends_when_the_face_is_lost() -> None:
    session, clock, _, judge = _live()
    _see(judge, clock, Sighting(face=FACE_RIGHT), frames=GLANCE_FACE_HITS)
    _see(judge, clock, Sighting(), frames=int(GLANCE_LOST_GRACE_S / STEP) + 1)
    assert not judge.glancing
    assert session.effective_input() == PilotInput()


def test_glance_waits_out_its_cooldown() -> None:
    _, clock, _, judge = _live()
    _see(judge, clock, Sighting(face=FACE_RIGHT), frames=GLANCE_FACE_HITS)
    judge.stop()
    _see(judge, clock, Sighting(face=FACE_RIGHT), frames=GLANCE_FACE_HITS)
    assert not judge.glancing
    clock.now += GLANCE_COOLDOWN_S
    _see(judge, clock, Sighting(face=FACE_RIGHT), frames=GLANCE_FACE_HITS)
    assert judge.glancing


def test_ai_motion_ends_a_glance() -> None:
    session, clock, _, judge = _live()
    _see(judge, clock, Sighting(face=FACE_RIGHT), frames=GLANCE_FACE_HITS)
    session.set_ai_intent(PilotInput(neck_pitch=0.3), 2.0)
    _see(judge, clock, Sighting(face=FACE_RIGHT))
    assert not judge.glancing
    assert session.effective_input() == PilotInput(neck_pitch=0.3)


class FakeWave:
    def __init__(self, waves: bool = False) -> None:
        self.waves = waves

    def process(self, frame: Any, timestamp: float) -> bool:
        return self.waves


class FakeFace:
    def locate(self, frame: Any) -> tuple[float, float] | None:
        return FACE_RIGHT


async def test_reflexes_run_waves_back_from_camera_frames() -> None:
    session, _, _, _ = _live()
    reflexes = Reflexes(session, lambda: object(), lambda: Detectors(FakeWave(True), FakeFace()), period=0.001)
    task = asyncio.create_task(reflexes.run())
    async with asyncio.timeout(2.0):
        while session.effective_input().gesture != "wave":
            await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_reflexes_end_quietly_when_detectors_cannot_load() -> None:
    # No mediapipe or no model download must leave the AI session running.
    session, _, _, _ = _live()

    def load() -> Detectors:
        raise ImportError("no mediapipe")

    await asyncio.wait_for(Reflexes(session, lambda: object(), load).run(), 2.0)


async def test_reflexes_survive_a_failing_frame_and_recenter_when_cancelled() -> None:
    session, clock, _, _ = _live()
    calls = 0

    def frames() -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("camera hiccup")
        return object()

    reflexes = Reflexes(session, frames, lambda: Detectors(FakeWave(), FakeFace()), period=0.001, now=clock)
    task = asyncio.create_task(reflexes.run())
    async with asyncio.timeout(2.0):
        while session.effective_input().neck_yaw == 0:
            await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert session.effective_input() == PilotInput()
