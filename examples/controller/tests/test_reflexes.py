"""Behavior of `palmimo_controller.ai.reflexes`: wave-back and face glances, judged on a fake clock."""

import asyncio
import json
from typing import Any

from palmimo_controller.ai.reflexes import (
    BOW_COOLDOWN_S,
    BOW_NEAR_FACE_FRACTION,
    BOW_NEAR_HITS,
    BOW_REARM_FACE_FRACTION,
    CAMERA_HFOV_DEG,
    GLANCE_COOLDOWN_S,
    GLANCE_DEADBAND,
    GLANCE_FACE_HITS,
    GLANCE_LOST_GRACE_S,
    GLANCE_MAX,
    GLANCE_SECONDS,
    GLANCE_UPDATE_S,
    GLANCE_YAW_SIGN,
    WAVE_COOLDOWN_S,
    Detectors,
    Reflexes,
    ReflexJudge,
    ReflexStats,
    Sighting,
)
from palmimo_controller.ai.vision import FaceBox
from palmimo_controller.session import AI_START_NOD_SECONDS, GESTURE_PLAY_SECONDS, PilotInput, PilotSession
from palmimo_controller.telemetry import Telemetry
from palmimo_sdk.engine import MotionEngine


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


FACE_RIGHT = (0.8, 0.5)
STEP = 0.1
NEAR = Sighting(face=(0.5, 0.5), face_size=BOW_NEAR_FACE_FRACTION + 0.1)
FAR = Sighting(face=(0.5, 0.5), face_size=BOW_NEAR_FACE_FRACTION / 2)


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


def _finish_bow(session: PilotSession, clock: FakeClock) -> None:
    clock.now += GESTURE_PLAY_SECONDS["bow"]
    assert not session.ai_busy


def test_close_face_is_bowed_to_after_a_few_frames() -> None:
    session, clock, events, judge = _live()
    _see(judge, clock, NEAR, frames=BOW_NEAR_HITS - 1)
    assert session.effective_input().gesture is None
    _see(judge, clock, NEAR)
    assert session.effective_input().gesture == "bow"
    assert events[-1]["reflex"] == "bow"


def test_a_face_that_is_not_close_never_draws_a_bow() -> None:
    session, clock, _, judge = _live()
    _see(judge, clock, FAR, frames=BOW_NEAR_HITS * 3)
    assert session.effective_input().gesture is None


def test_bow_fires_once_per_approach_and_rearms_after_moving_back() -> None:
    session, clock, _, judge = _live()
    _see(judge, clock, NEAR, frames=BOW_NEAR_HITS)
    _finish_bow(session, clock)
    clock.now += BOW_COOLDOWN_S
    # Still close, past the cooldown: the same approach is not bowed to again.
    _see(judge, clock, NEAR, frames=BOW_NEAR_HITS)
    assert session.effective_input().gesture is None
    _see(judge, clock, FAR)
    _see(judge, clock, NEAR, frames=BOW_NEAR_HITS)
    assert session.effective_input().gesture == "bow"


def test_bow_rearms_when_the_face_leaves() -> None:
    session, clock, _, judge = _live()
    _see(judge, clock, NEAR, frames=BOW_NEAR_HITS)
    _finish_bow(session, clock)
    clock.now += BOW_COOLDOWN_S
    _see(judge, clock, Sighting())
    _see(judge, clock, NEAR, frames=BOW_NEAR_HITS)
    assert session.effective_input().gesture == "bow"


def test_bow_waits_out_its_cooldown() -> None:
    session, clock, _, judge = _live()
    _see(judge, clock, NEAR, frames=BOW_NEAR_HITS)
    _finish_bow(session, clock)
    _see(judge, clock, FAR)
    _see(judge, clock, NEAR, frames=BOW_NEAR_HITS)
    assert session.effective_input().gesture is None
    clock.now += BOW_COOLDOWN_S
    _see(judge, clock, NEAR)
    assert session.effective_input().gesture == "bow"


def test_bow_yields_to_an_ai_intent_and_stays_armed() -> None:
    session, clock, _, judge = _live()
    session.set_ai_intent(PilotInput(move="forward"), 3.0)
    _see(judge, clock, NEAR, frames=BOW_NEAR_HITS)
    assert session.effective_input().move == "forward"
    clock.now += 3.0
    _see(judge, clock, NEAR)
    assert session.effective_input().gesture == "bow"


def test_wave_back_wins_over_a_bow_on_the_same_frame() -> None:
    session, clock, events, judge = _live()
    _see(judge, clock, NEAR, frames=BOW_NEAR_HITS - 1)
    _see(judge, clock, Sighting(waved=True, face=NEAR.face, face_size=NEAR.face_size))
    assert session.effective_input().gesture == "wave"
    assert "bow" not in [e["reflex"] for e in events]


def test_bow_ends_a_glance() -> None:
    session, clock, _, judge = _live()
    _see(judge, clock, Sighting(face=FACE_RIGHT), frames=GLANCE_FACE_HITS)
    assert judge.glancing
    _see(judge, clock, NEAR, frames=BOW_NEAR_HITS)
    assert not judge.glancing
    assert session.effective_input() == PilotInput(gesture="bow")


class FakeWave:
    def __init__(self, waves: bool = False) -> None:
        self.waves = waves

    def process(self, frame: Any, timestamp: float) -> bool:
        return self.waves


class FakeFace:
    def __init__(self, size: float = 0.1) -> None:
        self.size = size

    def locate_box(self, frame: Any) -> FaceBox | None:
        return FaceBox(center=FACE_RIGHT, size=self.size)


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


async def test_reflexes_bow_to_a_close_face_from_camera_frames() -> None:
    session, _, _, _ = _live()
    reflexes = Reflexes(session, lambda: object(), lambda: Detectors(FakeWave(), FakeFace(size=0.5)), period=0.001)
    task = asyncio.create_task(reflexes.run())
    async with asyncio.timeout(2.0):
        while session.effective_input().gesture != "bow":
            await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_bow_event_carries_the_face_size_and_rearm_says_why() -> None:
    _, clock, events, judge = _live()
    _see(judge, clock, NEAR, frames=BOW_NEAR_HITS)
    bow = next(event for event in events if event.get("reflex") == "bow")
    assert bow["face_size"] == round(NEAR.face_size or 0, 3) and bow["near_frames"] == BOW_NEAR_HITS
    assert bow["since_last_s"] is None
    _see(judge, clock, Sighting())
    rearm = events[-1]
    assert (rearm["reflex"], rearm["reason"]) == ("bow_rearm", "face_lost")


def test_glance_end_reports_its_reason() -> None:
    session, clock, events, judge = _live()
    _see(judge, clock, Sighting(face=FACE_RIGHT, face_size=0.1), frames=GLANCE_FACE_HITS)
    glance = next(event for event in events if event.get("reflex") == "glance")
    assert glance["face_size"] == 0.1 and glance["target_yaw"] > 0
    session.set_ai_intent(PilotInput(gesture="nod"), 1.0)
    _see(judge, clock, Sighting(face=FACE_RIGHT))
    end = events[-1]
    assert (end["reflex"], end["reason"]) == ("glance_end", "ai_intent")
    assert end["duration_s"] is not None


def test_reflex_stats_summarise_face_sizes_and_rate() -> None:
    stats = ReflexStats(started=0.0)
    for size in (0.1, 0.2, 0.3, 0.4, None):
        stats.record(Sighting(face_size=size))
    stats.record_error()
    summary = stats.summary(now=1.0)
    assert (summary["frames"], summary["fps"], summary["frame_errors"]) == (5, 5.0, 1)
    assert (summary["face_frames"], summary["face_size_max"], summary["near_frames"]) == (4, 0.4, 1)
    assert summary["face_size_p50"] == 0.3


async def test_reflexes_log_detector_load_and_final_stats() -> None:
    session, _, _, _ = _live()
    events: list[dict[str, Any]] = []
    reflexes = Reflexes(
        session,
        lambda: object(),
        lambda: Detectors(FakeWave(), FakeFace()),
        telemetry=Telemetry(emit=lambda line: events.append(json.loads(line))),
        period=0.001,
    )
    task = asyncio.create_task(reflexes.run())
    async with asyncio.timeout(2.0):
        while not session.gazing:
            await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    types = [event["type"] for event in events]
    assert types[0] == "reflex_ready" and "reflex_stats" in types


def test_glance_corrects_at_most_once_per_update_interval() -> None:
    # Without this, every frame would stack the same correction onto the neck.
    session, clock, _, judge = _live()
    _see(judge, clock, Sighting(face=FACE_RIGHT), frames=GLANCE_FACE_HITS)
    first = session.effective_input().neck_yaw
    frame = 1.0 / 15
    clock.now -= STEP  # back to the moment of the first correction
    for _ in range(int(GLANCE_UPDATE_S / frame) - 1):
        clock.now += frame
        judge.update(Sighting(face=FACE_RIGHT), clock.now)
    assert session.effective_input().neck_yaw == first


def test_glance_stops_at_its_cap_when_the_face_never_moves() -> None:
    # A face that never centers (a stale frame, a reversed yaw sign) used to
    # drive the neck into its mechanical stop; now it stops at GLANCE_MAX.
    session, clock, _, judge = _live()
    _see(judge, clock, Sighting(face=(0.95, 0.05)), frames=int(GLANCE_SECONDS / STEP) - 1)
    neck = session.effective_input()
    assert abs(neck.neck_yaw) == GLANCE_MAX and abs(neck.neck_pitch) == GLANCE_MAX


def test_glance_settles_on_the_face_despite_camera_delay() -> None:
    # The neck as on hardware: frames reach the judge 0.25 s late, and the
    # neck takes about a third of a second for its full travel. The old
    # every-frame correction ran to the stop under exactly this delay.
    session, clock, _, judge = _live()
    frame_s, delay_s, neck_speed = 1.0 / 15, 0.25, 3.0  # neck_speed: normalized travel per second
    face_deg = 15.0  # the visitor stands 15 degrees to the robot's right
    neck = 0.0
    history: list[tuple[float, float]] = []
    yaws: list[float] = []
    for _ in range(int(GLANCE_SECONDS / frame_s) - 1):
        target = session.effective_input().neck_yaw
        neck += max(-neck_speed * frame_s, min(neck_speed * frame_s, target - neck))
        history.append((clock.now, neck))
        seen = next((n for t, n in reversed(history) if t <= clock.now - delay_s), 0.0)
        offset = (face_deg - GLANCE_YAW_SIGN * seen * MotionEngine.NECK_YAW_TRAVEL_DEG) / CAMERA_HFOV_DEG
        judge.update(Sighting(face=(0.5 + offset, 0.5)), clock.now)
        yaws.append(session.effective_input().neck_yaw)
        clock.now += frame_s
    settled = face_deg / MotionEngine.NECK_YAW_TRAVEL_DEG
    assert max(abs(yaw) for yaw in yaws) < GLANCE_MAX
    assert (
        abs(yaws[-1] - GLANCE_YAW_SIGN * settled) < GLANCE_DEADBAND * CAMERA_HFOV_DEG / MotionEngine.NECK_YAW_TRAVEL_DEG
    )


def test_a_new_glance_reports_the_face_once() -> None:
    # The hook sends the model a frame of who just arrived; it must not fire every frame.
    session, clock, _, _ = _live()
    arrivals: list[float] = []
    judge = ReflexJudge(session, on_new_face=lambda: arrivals.append(clock.now))
    _see(judge, clock, Sighting(face=FACE_RIGHT), frames=GLANCE_FACE_HITS + 10)
    assert len(arrivals) == 1


def test_a_face_at_talking_distance_is_not_close_enough_to_bow() -> None:
    # Visitors talking to the robot measured 0.28-0.31; bowing at that size made it bow mid-conversation.
    _, clock, events, judge = _live()
    _see(judge, clock, Sighting(face=(0.5, 0.5), face_size=0.31), frames=BOW_NEAR_HITS * 4)
    assert not any(event.get("reflex") == "bow" for event in events)


def test_a_face_hovering_near_the_threshold_does_not_rearm_the_bow() -> None:
    session, clock, events, judge = _live()
    _see(judge, clock, NEAR, frames=BOW_NEAR_HITS)
    assert session.effective_input().gesture == "bow"
    session.set_ai_intent(PilotInput(), 0.0)  # the bow finished
    clock.now += BOW_COOLDOWN_S
    hovering = Sighting(face=(0.5, 0.5), face_size=BOW_REARM_FACE_FRACTION + 0.02)
    _see(judge, clock, hovering, frames=3)
    _see(judge, clock, NEAR, frames=BOW_NEAR_HITS)
    assert [event["reflex"] for event in events if event.get("reflex", "").startswith("bow")] == ["bow"]
    _see(judge, clock, FAR)
    _see(judge, clock, NEAR, frames=BOW_NEAR_HITS)
    assert session.effective_input().gesture == "bow"
