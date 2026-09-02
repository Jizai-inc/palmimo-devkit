"""Palmimo facade — motion commands, neck, choreography, compute-only step()."""

import threading
import time
from collections.abc import Iterator
from typing import Any, cast

import pytest

import palmimo_sdk.robot as robot_module
from palmimo_sdk import (
    FaceDisplay,
    FaceDisplayConnectTimeoutError,
    HeadCamera,
    Microphone,
    Motion,
    MotionCancelled,
    Palmimo,
    RoutineStep,
    ServoDriver,
    Speaker,
)
from palmimo_sdk.engine import MotionEngine
from palmimo_sdk.robot import NeckPitchDegrees, NeckPitchNormalized, NeckYawDegrees, NeckYawNormalized


def test_motion_commands_set_engine_motion() -> None:
    """Each command method switches the engine's motion."""
    robot = Palmimo()
    robot.forward()
    assert robot.motion == "forward"
    robot.dance()
    assert robot.motion == "dance"
    robot.stop()
    assert robot.motion == "idle"


def test_dance_knob_properties_roundtrip() -> None:
    """The dance knob properties pass through to the engine."""
    robot = Palmimo()
    robot.dance_speed = 0.02
    robot.dance_roll_deg = 7.0
    robot.dance_pivot_h = 130.0
    robot.dance_dwell = 0.3
    robot.dance_level_head = True
    assert robot.dance_speed == 0.02
    assert robot.dance_roll_deg == 7.0
    assert robot.dance_pivot_h == 130.0
    assert robot.dance_dwell == 0.3
    assert robot.dance_level_head is True
    assert robot.engine.dance_speed == 0.02  # reached the engine


def test_dance_sway_frames_end_on_an_extreme() -> None:
    """The sway frame count ends on an extreme (held side), not mid-swing, so the lingering hold lands there."""
    assert Palmimo._dance_sway_frames(0, 0.01) == 0  # nothing to sway
    assert Palmimo._dance_sway_frames(3, 0.0) == 0  # guard non-positive phase_inc
    for sways in (1, 2, 3, 4):
        pinc = 0.013
        n = Palmimo._dance_sway_frames(sways, pinc)
        end_phase = (n * pinc) % 1.0
        # Extremes live at phase 0.25 + k*0.5; the end must land on one of them.
        frac = (end_phase - 0.25) % 0.5
        assert min(frac, 0.5 - frac) < 0.02, f"sways={sways} end_phase={end_phase}"


def test_perform_dance_returns_to_neutral_and_idles() -> None:
    """perform_dance returns to neutral after the routine and ends in IDLE (compute-only)."""
    robot = Palmimo()  # compute-only: no real-time glide needed
    pos = robot.perform_dance(sways=0, end_hold=0.0, settle=0.0)
    assert len(pos) == 20
    assert robot.motion == "idle"
    # Eased home. neck_pitch1 settles at the trimmed rest center
    # (neck_rest_pitch_deg, introduced with the nod/head-shake gestures), not
    # raw servo neutral — same target return_to_neutral uses.
    center = robot._engine.neck_pitch_center()
    for name, t in pos.items():
        expected = center if name == "neck_pitch1" else 2048
        assert abs(t - expected) <= 2, (name, t, expected)


def test_perform_dance_cancelled_during_sway_leaves_motion_idle() -> None:
    """cancel() during the sway section aborts perform_dance() (raising
    MotionCancelled) but stop() still runs first, so the facade never stays
    parked in Motion.DANCE afterward."""
    calls = {"n": 0}

    def on_step(_pos: dict[str, int]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            robot.cancel()

    robot = Palmimo(fps=1000, on_step=on_step)
    with pytest.raises(MotionCancelled):
        robot.perform_dance(sways=5, end_hold=0.0, settle=0.0)
    assert robot.motion == "idle"


def test_perform_dance_cancelled_during_end_hold_leaves_motion_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """cancel() during the (compute-only, no driver) end-hold sleep aborts
    perform_dance() and still leaves motion idle -- the end-hold section
    used to be an uninterruptible time.sleep()."""
    import time as _time

    robot = Palmimo(fps=1000, on_step=lambda _pos: None)

    real_sleep = _time.sleep
    calls = {"n": 0}

    def fake_sleep(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            robot.cancel()
        real_sleep(min(seconds, 0.001))

    monkeypatch.setattr(robot_module.time, "sleep", fake_sleep)
    with pytest.raises(MotionCancelled):
        robot.perform_dance(sways=0, end_hold=1.0, settle=0.0)
    assert robot.motion == "idle"
    assert calls["n"] >= 1


def test_perform_dance_cancelled_during_settle_glide_leaves_motion_idle() -> None:
    """cancel() during a driver-backed timed settle glide aborts
    perform_dance() and still leaves motion idle -- the settle glide used to
    be an uninterruptible driver loop."""

    class _CancelOnFirstWriteDriver:
        is_connected = True

        def __init__(self) -> None:
            self.writes = 0

        def read_positions(self) -> dict[str, int]:
            names = [f"leg_{i}_{s}" for i in range(1, 7) for s in ("yaw", "pitch1", "pitch2")]
            names += ["neck_yaw", "neck_pitch1"]
            return dict.fromkeys(names, 2048)

        def write_positions(self, positions: dict[str, int]) -> None:
            self.writes += 1
            if self.writes == 1:
                robot.cancel()

    driver = _CancelOnFirstWriteDriver()
    robot = Palmimo(driver=cast(ServoDriver, driver), fps=1000)
    with pytest.raises(MotionCancelled):
        robot.perform_dance(sways=0, end_hold=0.0, settle=0.2)
    assert robot.motion == "idle"


def test_step_returns_positions() -> None:
    """step() returns a position dict for all 20 motors."""
    robot = Palmimo()
    robot.forward()
    pos = robot.step()
    assert len(pos) == 20


def test_on_step_callback_is_invoked() -> None:
    """The on_step callback is invoked on every step()."""
    seen: list[dict[str, int]] = []
    robot = Palmimo(on_step=seen.append)
    robot.forward()
    robot.step_n(5)
    assert len(seen) == 5
    assert all(len(p) == 20 for p in seen)


def test_cancel_raises_motion_cancelled_mid_run() -> None:
    """cancel() aborts an in-flight run(), raising MotionCancelled — checked
    once per frame inside _pace, so it lands well before all steps complete."""
    seen: list[dict[str, int]] = []

    def on_step(pos: dict[str, int]) -> None:
        seen.append(pos)
        if len(seen) == 1:
            robot.cancel()

    robot = Palmimo(fps=1000, on_step=on_step)  # fast fps keeps the test quick
    robot.forward()
    with pytest.raises(MotionCancelled):
        robot.run(steps=50)
    assert len(seen) < 50  # aborted well short of the requested step count


def test_cancel_while_idle_does_not_carry_over_to_next_run() -> None:
    """A cancel() delivered with no run() in flight does not cancel the NEXT run()."""
    robot = Palmimo(fps=1000)
    robot.cancel()  # nothing running yet
    robot.forward()
    pos = robot.run(steps=5)  # must NOT raise: run()'s entry snapshot postdates the idle cancel
    assert len(pos) == 20
    assert robot.motion == "forward"


def test_run_snapshot_ignores_cancel_count_bumped_before_it_started() -> None:
    """A cancel() delivered before run() started doesn't count against the
    snapshot run() takes at its own entry -- the counter+snapshot scheme
    (see cancel()'s docstring) means only a cancel() AFTER entry can ever
    raise MotionCancelled inside this call."""
    robot = Palmimo(fps=1000)
    robot._cancel_count += 1  # a stale cancel from a completely separate earlier call
    robot.forward()
    pos = robot.run(steps=3)
    assert len(pos) == 20


def test_pace_ignores_cancels_that_predate_its_snapshot() -> None:
    """_pace() only reacts to cancel()s that ran AFTER the snapshot passed to
    it -- a snapshot taken after prior cancel() calls have already landed
    does not retroactively cancel this call. run(), perform_dance(), and
    play_realtime() all take their snapshot at their own entry, so they
    share this same "cancel only affects an in-flight call" semantics."""
    robot = Palmimo()
    robot.cancel()
    robot.cancel()
    snapshot = robot._cancel_count  # taken AFTER the cancels above
    last = robot._pace(iter([{"a": 1}, {"a": 2}]), fps=1000, cancel_snapshot=snapshot)
    assert last == {"a": 2}


def test_pace_raises_motion_cancelled_when_cancelled_mid_loop() -> None:
    """A cancel() arriving while _pace() is driving frames aborts at the next
    frame boundary, whichever paced entry point is running."""
    robot = Palmimo()
    snapshot = robot._cancel_count

    def frames() -> "Iterator[dict[str, int]]":
        yield {"a": 1}
        robot.cancel()
        yield {"a": 2}
        yield {"a": 3}

    with pytest.raises(MotionCancelled):
        robot._pace(frames(), fps=1000, cancel_snapshot=snapshot)


def test_cancel_between_run_entry_and_first_frame_is_never_lost() -> None:
    """The counter+snapshot design closes the historical clear()/set() race:
    a cancel() delivered right after run() takes its snapshot but before the
    very first frame is processed must still raise MotionCancelled. An
    Event-based design that cleared itself at the pacing loop's own entry
    could lose a cancel() landing in exactly this window (a concurrent
    set() racing the clear()); the counter has no clear() step to race
    against, so this can't happen here."""
    robot = Palmimo(fps=1000)
    original_step = robot.step
    calls = {"n": 0}

    def step_and_cancel_once() -> dict[str, int]:
        calls["n"] += 1
        if calls["n"] == 1:
            # Arrives after run()'s snapshot was already taken, before the
            # pacing loop's very first cancellation check.
            robot.cancel()
        return original_step()

    robot.step = step_and_cancel_once  # type: ignore[method-assign]  # deliberate monkeypatch to inject the race
    robot.forward()
    with pytest.raises(MotionCancelled):
        robot.run(steps=100)
    assert calls["n"] == 1  # aborted right after the very first frame


# ----------------------------------------------------------------------
# _arm_cancel_scope() / _disarm_cancel_scope() / _take_cancel_snapshot():
# closing the dispatch-to-entry cancel window.
# ----------------------------------------------------------------------


def test_arm_cancel_scope_records_current_count_and_disarm_clears_it() -> None:
    robot = Palmimo()
    robot.cancel()
    robot._arm_cancel_scope()
    assert robot._armed_snapshot == robot._cancel_count
    robot._disarm_cancel_scope()
    assert robot._armed_snapshot is None


def test_take_cancel_snapshot_consumes_and_clears_an_armed_scope() -> None:
    """_take_cancel_snapshot() returns the armed value (not a fresh read of
    the live counter) and resets the armed slot so it is consumed exactly
    once."""
    robot = Palmimo()
    robot._arm_cancel_scope()
    armed = robot._armed_snapshot
    robot.cancel()  # arrives after arming -- must still count as "after entry"
    assert robot._take_cancel_snapshot() == armed
    assert robot._armed_snapshot is None
    # A second call with nothing armed just reads the live counter.
    assert robot._take_cancel_snapshot() == robot._cancel_count


def test_armed_scope_catches_cancel_that_lands_before_run_entry() -> None:
    """A cancel() delivered after _arm_cancel_scope() but before run() is
    even entered must still raise MotionCancelled inside that run() --
    the structural fix for the "dispatch to run() entry" absorption window
    documented on cancel()."""
    robot = Palmimo(fps=1000)
    robot._arm_cancel_scope()
    robot.cancel()  # after arming, strictly before run() is entered
    robot.forward()
    with pytest.raises(MotionCancelled):
        robot.run(steps=50)


def test_armed_scope_catches_cancel_from_another_thread_before_run_entry() -> None:
    """Same contract as above, but the cancel() actually arrives from another
    thread -- the scenario _arm_cancel_scope() exists for (a caller about to
    dispatch run() onto a worker thread)."""
    robot = Palmimo(fps=1000)
    robot.forward()
    robot._arm_cancel_scope()

    cancelled = threading.Event()

    def cancel_from_other_thread() -> None:
        robot.cancel()
        cancelled.set()

    t = threading.Thread(target=cancel_from_other_thread)
    t.start()
    t.join(timeout=2.0)
    assert cancelled.is_set()

    with pytest.raises(MotionCancelled):
        robot.run(steps=50)


def test_disarm_cancel_scope_prevents_leaking_into_next_run() -> None:
    """Disarming resets the armed slot, so the historical "cancel while idle
    does not carry over" semantics are unaffected for a caller that never
    dispatches the armed call at all."""
    robot = Palmimo(fps=1000)
    robot._arm_cancel_scope()
    robot._disarm_cancel_scope()
    robot.cancel()  # unarmed at this point -- an ordinary idle cancel
    robot.forward()
    pos = robot.run(steps=5)  # must NOT raise
    assert len(pos) == 20


def test_unarmed_run_semantics_are_unchanged() -> None:
    """A direct run() call that never arms a scope behaves exactly like
    before: _take_cancel_snapshot() with nothing armed is just the live
    counter, identical to the old `self._cancel_count` read."""
    robot = Palmimo(fps=1000)
    assert robot._armed_snapshot is None
    robot.cancel()  # idle cancel, no scope armed
    robot.forward()
    pos = robot.run(steps=5)  # must NOT raise
    assert len(pos) == 20


# ----------------------------------------------------------------------
# cancel_checkpoint() / raise_if_cancelled(): the public counterparts of
# _take_cancel_snapshot() / _check_cancelled() for a hand-rolled control loop.
# ----------------------------------------------------------------------


def test_cancel_checkpoint_matches_take_cancel_snapshot() -> None:
    """cancel_checkpoint() is a thin public delegate to _take_cancel_snapshot()."""
    robot = Palmimo()
    robot.cancel()
    checkpoint = robot.cancel_checkpoint()
    assert checkpoint == robot._cancel_count


def test_raise_if_cancelled_raises_only_after_the_checkpoint() -> None:
    robot = Palmimo()
    checkpoint = robot.cancel_checkpoint()
    robot.raise_if_cancelled(checkpoint)  # nothing happened yet -- must not raise
    robot.cancel()
    with pytest.raises(MotionCancelled):
        robot.raise_if_cancelled(checkpoint)


def test_cancel_checkpoint_consumes_an_armed_scope() -> None:
    """cancel_checkpoint() shares _take_cancel_snapshot()'s armed-scope
    consumption, so a caller that armed a scope before dispatching a
    hand-rolled loop onto another thread still closes that dispatch-to-entry
    window."""
    robot = Palmimo()
    robot._arm_cancel_scope()
    armed = robot._armed_snapshot
    robot.cancel()  # arrives after arming, strictly before the checkpoint call
    checkpoint = robot.cancel_checkpoint()
    assert checkpoint == armed
    with pytest.raises(MotionCancelled):
        robot.raise_if_cancelled(checkpoint)


def test_bow_command_and_alias() -> None:
    """bow() and set_motion("bow") both select Motion.BOW."""
    robot = Palmimo()
    robot.bow()
    assert robot.motion == "bow"
    robot.stop()
    robot.set_motion("bow")
    assert robot.motion == "bow"


def test_stretch_command_and_alias() -> None:
    """stretch() and set_motion("stretch") both select Motion.STRETCH."""
    robot = Palmimo()
    robot.stretch()
    assert robot.motion == "stretch"
    robot.stop()
    robot.set_motion("stretch")
    assert robot.motion == "stretch"


def test_set_motion_rejects_unknown_name() -> None:
    """An unknown motion name raises ValueError."""
    robot = Palmimo()
    with pytest.raises(ValueError, match="Unknown motion"):
        robot.set_motion("moonwalk")


def test_wave_both_command_and_knobs_pass_through() -> None:
    """wave_both() and the _WAVE_BOTH knobs pass straight through to the engine."""
    robot = Palmimo()
    robot.wave_both()
    assert robot.motion == "wave_both"
    robot.wave_both_lean = 12
    robot.wave_both_noseup = 10
    robot.wave_both_mid_forward = 40
    robot.wave_both_yaw = 25
    robot.wave_both_size = 130
    robot.wave_both_intro_speed = 1.5
    robot.wave_both_phase = 0.25
    robot.wave_both_speed = 2.2
    robot.wave_both_decay = 0.85
    eng = robot._engine
    assert (
        eng.wave_both_lean,
        eng.wave_both_noseup,
        eng.wave_both_mid_forward,
        eng.wave_both_yaw,
        eng.wave_both_size,
        eng.wave_both_intro_speed,
        eng.wave_both_phase,
        eng.wave_both_speed,
        eng.wave_both_decay,
    ) == (12.0, 10.0, 40.0, 25.0, 130.0, 1.5, 0.25, 2.2, 0.85)
    # the read-back also reflects the engine's value
    assert robot.wave_both_mid_forward == 40.0


def test_clap_command_and_knobs_pass_through() -> None:
    """clap() / set_motion("clap") and the _CLAP knobs pass straight through to the engine."""
    robot = Palmimo()
    robot.clap()
    assert robot.motion == "clap"
    robot.stop()
    robot.set_motion("clap")
    assert robot.motion == "clap"
    robot.clap_count = 4
    robot.clap_period = 0.5
    robot.clap_gap = 20
    robot.clap_open = 120
    robot.clap_height = 100
    robot.clap_dwell = 0.2
    robot.clap_decay = 0.8
    robot.clap_intro_speed = 1.5
    eng = robot._engine
    assert (
        eng.clap_count,
        eng.clap_period,
        eng.clap_gap,
        eng.clap_open,
        eng.clap_height,
        eng.clap_dwell,
        eng.clap_decay,
        eng.clap_intro_speed,
    ) == (4, 0.5, 20.0, 120.0, 100.0, 0.2, 0.8, 1.5)
    assert robot.clap_gap == 20.0


def test_look_accepts_plain_float_as_normalized_backward_compat() -> None:
    """look()'s plain float arguments are still treated as normalized values (backward compat)."""
    robot = Palmimo()
    robot.look(pitch=0.5, yaw=-0.25)
    assert robot._engine._neck_target_pitch == pytest.approx(0.5)
    assert robot._engine._neck_target_yaw == pytest.approx(-0.25)


def test_look_accepts_normalized_value_object_passthrough() -> None:
    """NeckPitchNormalized/NeckYawNormalized pass through as normalized values (same as the float path)."""
    robot = Palmimo()
    robot.look(pitch=NeckPitchNormalized(0.5), yaw=NeckYawNormalized(-0.25))
    assert robot._engine._neck_target_pitch == pytest.approx(0.5)
    assert robot._engine._neck_target_yaw == pytest.approx(-0.25)


@pytest.mark.parametrize("bad_value", [-1.01, 1.01, 2.0])
def test_neck_pitch_normalized_rejects_out_of_range_at_construction(bad_value: float) -> None:
    """NeckPitchNormalized validates the [-1, 1] range at construction (the value object owns its own validation)."""
    with pytest.raises(ValueError, match=r"\[-1, 1\]"):
        NeckPitchNormalized(bad_value)


@pytest.mark.parametrize("bad_value", [-1.01, 1.01, 2.0])
def test_neck_yaw_normalized_rejects_out_of_range_at_construction(bad_value: float) -> None:
    """NeckYawNormalized validates the [-1, 1] range at construction."""
    with pytest.raises(ValueError, match=r"\[-1, 1\]"):
        NeckYawNormalized(bad_value)


def test_neck_normalized_accepts_boundary_values() -> None:
    """Exactly ±1.0 is accepted as a boundary value."""
    assert NeckPitchNormalized(1.0).value == 1.0
    assert NeckPitchNormalized(-1.0).value == -1.0
    assert NeckYawNormalized(1.0).value == 1.0
    assert NeckYawNormalized(-1.0).value == -1.0


def test_look_accepts_degrees_and_converts_via_real_neck_travel() -> None:
    """NeckPitchDegrees/NeckYawDegrees normalize by dividing by the engine's real range of
    motion (the per-axis *_TRAVEL_DEG), not a fixed angle.

    Specifying half the travel angle normalizes to 0.5, since the conversion is
    anchored to the actual range of motion.
    """
    robot = Palmimo()
    pitch_travel = MotionEngine.NECK_PITCH_TRAVEL_DEG
    yaw_travel = MotionEngine.NECK_YAW_TRAVEL_DEG
    robot.look(pitch=NeckPitchDegrees(pitch_travel / 2), yaw=NeckYawDegrees(-yaw_travel / 2))
    assert robot._engine._neck_target_pitch == pytest.approx(0.5)
    assert robot._engine._neck_target_yaw == pytest.approx(-0.5)


def test_look_degrees_at_full_travel_reaches_normalized_one() -> None:
    """Degrees exactly at the real range of motion normalize to ±1.0."""
    robot = Palmimo()
    pitch_travel = MotionEngine.NECK_PITCH_TRAVEL_DEG
    yaw_travel = MotionEngine.NECK_YAW_TRAVEL_DEG
    robot.look(pitch=NeckPitchDegrees(pitch_travel), yaw=NeckYawDegrees(-yaw_travel))
    assert robot._engine._neck_target_pitch == pytest.approx(1.0)
    assert robot._engine._neck_target_yaw == pytest.approx(-1.0)


def test_neck_pitch_degrees_beyond_travel_raises_value_error_at_construction() -> None:
    """A NeckPitchDegrees value beyond the real range of motion raises ValueError instead of
    saturating (the value object owns its own validation)."""
    travel = MotionEngine.NECK_PITCH_TRAVEL_DEG
    with pytest.raises(ValueError, match="pitch"):
        NeckPitchDegrees(travel + 0.001)
    with pytest.raises(ValueError, match="pitch"):
        NeckPitchDegrees(-(travel + 0.001))


def test_neck_yaw_degrees_beyond_travel_raises_value_error_at_construction() -> None:
    """A NeckYawDegrees value beyond the real range of motion raises ValueError instead of saturating."""
    travel = MotionEngine.NECK_YAW_TRAVEL_DEG
    with pytest.raises(ValueError, match="yaw"):
        NeckYawDegrees(travel + 0.001)
    with pytest.raises(ValueError, match="yaw"):
        NeckYawDegrees(-(travel + 0.001))


def test_neck_degrees_accepts_boundary_value_exactly_at_travel() -> None:
    """A value exactly at the real range of motion is accepted as a boundary value (not
    rejected by floating-point rounding)."""
    pitch_travel = MotionEngine.NECK_PITCH_TRAVEL_DEG
    yaw_travel = MotionEngine.NECK_YAW_TRAVEL_DEG
    assert NeckPitchDegrees(pitch_travel).value == pitch_travel
    assert NeckPitchDegrees(-pitch_travel).value == -pitch_travel
    assert NeckYawDegrees(yaw_travel).value == yaw_travel
    assert NeckYawDegrees(-yaw_travel).value == -yaw_travel


def test_look_rejects_yaw_value_object_passed_as_pitch() -> None:
    """A value object for the wrong axis (a yaw value passed as pitch) is clearly rejected with TypeError."""
    robot = Palmimo()
    with pytest.raises(TypeError, match="YAW"):
        robot.look(pitch=NeckYawDegrees(5.0))  # type: ignore[arg-type]  # deliberately the wrong axis
    with pytest.raises(TypeError, match="YAW"):
        robot.look(pitch=NeckYawNormalized(0.5))  # type: ignore[arg-type]  # deliberately the wrong axis


def test_look_rejects_pitch_value_object_passed_as_yaw() -> None:
    """A value object for the wrong axis (a pitch value passed as yaw) is clearly rejected with TypeError."""
    robot = Palmimo()
    with pytest.raises(TypeError, match="PITCH"):
        robot.look(yaw=NeckPitchDegrees(5.0))  # type: ignore[arg-type]  # deliberately the wrong axis
    with pytest.raises(TypeError, match="PITCH"):
        robot.look(yaw=NeckPitchNormalized(0.5))  # type: ignore[arg-type]  # deliberately the wrong axis


def test_look_default_args_are_normalized_zero() -> None:
    """look() with no arguments is treated as normalized 0.0 (center)."""
    robot = Palmimo()
    robot.look()
    assert robot._engine._neck_target_pitch == pytest.approx(0.0)
    assert robot._engine._neck_target_yaw == pytest.approx(0.0)


def test_look_center_returns_neck_to_center() -> None:
    """The neck returns to neutral after look_center()."""
    robot = Palmimo()
    robot.look(pitch=1.0, yaw=-1.0)
    robot.step_n(40)
    robot.look_center()
    robot.step_n(40)
    pos = robot.positions
    assert pos["neck_pitch1"] == robot.engine.neck_pitch_center()  # front-facing includes the rest trim
    assert pos["neck_yaw"] == 2048


def test_play_yields_expected_frame_count() -> None:
    """play() yields round(duration * fps) frames (legacy tuple path)."""
    robot = Palmimo()
    frames = list(robot.play([("forward", 0.2), ("look_around", 0.2)], fps=60))
    assert len(frames) == round(0.2 * 60) * 2
    assert all(len(f) == 20 for f in frames)


def test_fps_and_dt_defaults() -> None:
    """The default control rate is 60fps, with dt = 1/60."""
    robot = Palmimo()
    assert robot.fps == 60
    assert robot.dt == pytest.approx(1 / 60)


def test_fps_is_customizable() -> None:
    """Specifying fps updates dt to match."""
    robot = Palmimo(fps=120)
    assert robot.fps == 120
    assert robot.dt == pytest.approx(1 / 120)


def test_non_positive_fps_rejected() -> None:
    """fps <= 0 raises ValueError."""
    with pytest.raises(ValueError, match="fps must be positive"):
        Palmimo(fps=0)


def test_run_steps_advances_expected_cycles() -> None:
    """run(steps=n) advances n control cycles and returns the final position."""
    seen: list[dict[str, int]] = []
    robot = Palmimo(on_step=seen.append, fps=1000)  # high fps minimizes sleep time
    robot.forward()
    final = robot.run(steps=10)
    assert len(seen) == 10
    assert len(final) == 20


def test_run_seconds_converts_via_fps() -> None:
    """run(seconds=) converts to round(seconds * fps) cycles."""
    seen: list[dict[str, int]] = []
    robot = Palmimo(on_step=seen.append, fps=100)
    robot.forward()
    robot.run(seconds=0.05)  # 0.05 * 100 = 5 cycles
    assert len(seen) == 5


def test_run_requires_exactly_one_argument() -> None:
    """Exactly one of seconds / steps must be given; both or neither raises ValueError."""
    robot = Palmimo()
    with pytest.raises(ValueError, match="exactly one"):
        robot.run()
    with pytest.raises(ValueError, match="exactly one"):
        robot.run(seconds=1.0, steps=10)


def test_run_rejects_negative_values() -> None:
    """A negative duration / step count raises ValueError."""
    robot = Palmimo()
    with pytest.raises(ValueError, match="seconds must be"):
        robot.run(seconds=-0.1)
    with pytest.raises(ValueError, match="steps must be"):
        robot.run(steps=-1)


def test_run_zero_returns_pose_without_stepping() -> None:
    """Zero cycles returns the current pose without stepping."""
    seen: list[dict[str, int]] = []
    robot = Palmimo(on_step=seen.append)
    pos = robot.run(steps=0)
    assert seen == []
    assert len(pos) == 20


def test_run_is_paced_by_control_rate() -> None:
    """run paces itself in real time according to fps (checked against a loose lower bound)."""
    robot = Palmimo(fps=200)
    robot.forward()
    start = time.perf_counter()
    robot.run(steps=20)  # expected ~= 0.1s
    elapsed = time.perf_counter() - start
    assert elapsed >= 0.05  # a generous lower bound that tolerates scheduler jitter


def test_play_typed_routine_frame_count() -> None:
    """A RoutineStep routine also yields round(duration * fps) frames."""
    robot = Palmimo()
    routine = [RoutineStep(Motion.FORWARD, 0.2), RoutineStep(Motion.DANCE, 0.1)]
    frames = list(robot.play(routine, fps=60))
    assert len(frames) == round(0.2 * 60) + round(0.1 * 60)
    assert all(len(f) == 20 for f in frames)


def test_run_and_play_agree_on_duration_to_steps() -> None:
    """The same duration's seconds->steps conversion agrees between run and play (both use round)."""
    seconds, fps = 0.125, 60  # 0.125 * 60 = 7.5 -> round=8 (int truncation would give 7, off by one)
    run_seen: list[dict[str, int]] = []
    runner = Palmimo(on_step=run_seen.append, fps=fps)
    runner.forward()
    runner.run(seconds=seconds)
    play_frames = list(Palmimo(fps=fps).play([RoutineStep(Motion.FORWARD, seconds)]))
    assert len(run_seen) == 8
    assert len(play_frames) == 8


def test_play_realtime_is_paced_by_control_rate() -> None:
    """play_realtime also paces itself in real time according to fps (checked against a loose lower bound)."""
    robot = Palmimo(fps=200)
    start = time.perf_counter()
    robot.play_realtime([RoutineStep(Motion.FORWARD, 0.1)])  # 20 frames ≈ 0.1s
    elapsed = time.perf_counter() - start
    assert elapsed >= 0.05


def test_play_realtime_fires_on_step_once_per_frame() -> None:
    """play_realtime fires on_step exactly once per frame (no double-firing)."""
    seen: list[dict[str, int]] = []
    robot = Palmimo(on_step=seen.append, fps=200)
    robot.play_realtime([RoutineStep(Motion.FORWARD, 0.05)])  # round(0.05 * 200) = 10
    assert len(seen) == 10


def test_play_defaults_to_facade_fps() -> None:
    """When play's fps is omitted, it uses the facade's fps."""
    robot = Palmimo(fps=30)
    frames = list(robot.play([RoutineStep(Motion.FORWARD, 1.0)]))
    assert len(frames) == 30


def test_neck_sweep_keeps_leg_motion_and_recenters() -> None:
    """neck_sweep keeps the leg motion running while sweeping the neck, then recenters when the cue ends."""
    robot = Palmimo()
    list(robot.play([RoutineStep(Motion.FORWARD, 0.3, neck_sweep=True)], fps=60))
    assert robot.motion == "forward"  # leg motion is preserved
    robot.stop()
    robot.step_n(60)  # after look_center, the neck settles to front-facing (including the rest trim)
    assert robot.positions["neck_yaw"] == 2048
    assert robot.positions["neck_pitch1"] == robot.engine.neck_pitch_center()


def test_play_rejects_unknown_legacy_name() -> None:
    """An unknown motion name in the legacy tuple form raises ValueError."""
    robot = Palmimo()
    with pytest.raises(ValueError, match="Unknown motion"):
        list(robot.play([("moonwalk", 0.1)]))


# ================================================================
# Neck gestures (nod / head_shake)
# ================================================================


def test_nod_and_head_shake_commands() -> None:
    """nod() / head_shake() switch the engine's motion."""
    robot = Palmimo()
    robot.nod()
    assert robot.motion == "nod"
    robot.head_shake()
    assert robot.motion == "head_shake"


def test_set_motion_accepts_gesture_names_and_alias() -> None:
    """set_motion accepts nod / head_shake / shake_head (an alias)."""
    robot = Palmimo()
    robot.set_motion("nod")
    assert robot.motion == "nod"
    robot.set_motion("shake_head")  # alias
    assert robot.motion == "head_shake"
    robot.set_motion("head_shake")
    assert robot.motion == "head_shake"


def test_gesture_knobs_delegate_to_engine() -> None:
    """The nod_* / shake_* knobs delegate to the engine with type conversion."""
    robot = Palmimo()
    robot.nod_amp_deg = 12
    robot.nod_period = 0.5
    robot.nod_count = 3
    robot.nod_decay = 0.7
    robot.shake_amp_deg = 15
    robot.shake_period = 0.35
    robot.shake_swings = 3
    robot.shake_decay = 0.9
    e = robot.engine
    assert (e.nod_amp_deg, e.nod_period, e.nod_count, e.nod_decay) == (12.0, 0.5, 3, 0.7)
    assert (e.shake_amp_deg, e.shake_period, e.shake_swings, e.shake_decay) == (15.0, 0.35, 3, 0.9)
    assert isinstance(e.nod_count, int) and isinstance(e.shake_swings, int)


class _PVRecorder:
    """Minimal driver stub that records set_profile_velocity_units calls."""

    is_connected = True

    def __init__(self) -> None:
        self.calls: list[tuple[int, tuple[str, ...] | None]] = []

    def write_positions(self, positions: dict[str, int]) -> None:
        pass

    def set_profile_velocity_units(self, value: int, motors: list[str] | None = None) -> None:
        self.calls.append((value, tuple(motors) if motors is not None else None))


def test_neck_gesture_pv_applied_and_restored() -> None:
    """During a gesture the two neck axes get PV=0, restored to the default PV on exit (same shape as wave tuning)."""
    driver = _PVRecorder()
    robot = Palmimo(driver=cast(ServoDriver, driver))
    robot.step()  # idle — no tuning traffic
    assert driver.calls == []
    robot.nod()
    robot.step()  # gesture enter -> PV=0 on the neck axes
    assert driver.calls == [(0, ("neck_pitch1", "neck_yaw"))]
    robot.step()  # steady state -> no re-write
    assert len(driver.calls) == 1
    robot.stop()
    robot.step()  # gesture exit -> default PV restored
    assert driver.calls[-1] == (300, ("neck_pitch1", "neck_yaw"))


def test_return_to_neutral_converges_with_rest_trim() -> None:
    """return_to_neutral converges to a pose that includes the neck's rest trim, without spinning to the safety cap."""
    seen: list[dict[str, int]] = []
    robot = Palmimo(on_step=seen.append)
    robot.forward()
    robot.step_n(30)
    robot.return_to_neutral()
    assert len(seen) - 30 < 120  # convergence detection kicks in (doesn't run to the cap=120)
    pos = robot.positions
    assert pos["neck_pitch1"] == robot.engine.neck_pitch_center()
    assert all(abs(t - 2048) <= 2 for n, t in pos.items() if n != "neck_pitch1")


class _GlideDriver:
    """Minimal driver with read/write/PV support, for verifying the timed-glide and wake paths."""

    is_connected = True

    def __init__(self, pose: int = 2048) -> None:
        self.pose = pose
        self.writes: list[dict[str, int]] = []
        self.pv_calls: list[tuple[int, tuple[str, ...] | None]] = []
        self.profile_velocity = 600  # a driver with a non-default PV

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def read_positions(self) -> dict[str, int]:
        names = [f"leg_{i}_{s}" for i in range(1, 7) for s in ("yaw", "pitch1", "pitch2")]
        names += ["neck_yaw", "neck_pitch1"]
        return dict.fromkeys(names, self.pose)

    def write_positions(self, positions: dict[str, int]) -> None:
        self.writes.append(dict(positions))

    def set_profile_velocity_units(self, value: int, motors: list[str] | None = None) -> None:
        self.pv_calls.append((value, tuple(motors) if motors is not None else None))


def test_timed_return_seeds_neck_so_first_nod_does_not_snap(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a timed glide, the engine's neck is synced to the real pose, so the first nod doesn't snap."""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    driver = _GlideDriver()
    robot = Palmimo(driver=cast(ServoDriver, driver))
    robot.return_to_neutral(duration=0.1)
    rest = robot.engine.neck_pitch_center()
    # the engine's neck is seeded to the glide target (the rest-trim position)
    assert robot.positions["neck_pitch1"] == rest
    # the first frame of the initial nod starts near rest, not a snap away from it
    robot.nod()
    first = robot.step()
    assert abs(first["neck_pitch1"] - rest) < 20


def test_wake_targets_trimmed_neck_front(monkeypatch: pytest.MonkeyPatch) -> None:
    """wake()'s glide target is the neck's rest-trim position, not raw center (avoids a bow-like head bounce)."""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    driver = _GlideDriver(pose=1900)
    robot = Palmimo(driver=cast(ServoDriver, driver))
    robot.wake(duration=0.1)
    last = driver.writes[-1]
    assert abs(last["neck_pitch1"] - robot.engine.neck_pitch_center()) <= 1
    assert abs(last["leg_1_yaw"] - 2048) <= 1


class _GainGlideDriver(_GlideDriver):
    """_GlideDriver that can also ramp Position_P_Gain, for the sleep/wake path."""

    def __init__(self, pose: int = 2048) -> None:
        super().__init__(pose)
        self.gain_calls: list[tuple[int | None, tuple[str, ...] | None]] = []

    def set_position_p_gain(self, value: int | None, motors: list[str] | None = None) -> None:
        self.gain_calls.append((value, tuple(motors) if motors is not None else None))


def test_sleep_ramps_the_legs_down_to_where_wake_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two have to meet: sleep's end gain is wake's start gain, or waking
    would either jolt (gap upward) or sag further (gap downward)."""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    driver = _GainGlideDriver()
    robot = Palmimo(driver=cast(ServoDriver, driver))

    robot.sleep(duration=0.1)

    written = [value for value, motors in driver.gain_calls if motors and "leg_1_yaw" in motors]
    assert written, "the legs never had their gain written"
    # None means "restore the driver default", which the ramp must never do:
    # it would hand the legs full stiffness partway down.
    assert all(gain is not None for gain in written), "the ramp wrote a restore-to-default"
    leg_gains = cast(list[int], written)
    assert leg_gains == sorted(leg_gains, reverse=True), "the leg gain must fall, not rise"
    # Read the constant rather than repeat it: a literal here would keep passing
    # if wake's start moved, which is the one thing this test exists to catch.
    assert leg_gains[-1] == robot_module._WAKE_START_GAIN


def test_sleep_softens_the_neck_last_so_the_head_eases_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """The neck is released after the glide, not during it -- softening it mid-glide
    drops the head while the body is still moving."""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    driver = _GainGlideDriver()
    robot = Palmimo(driver=cast(ServoDriver, driver))

    robot.sleep(duration=0.1)

    neck_indices = [i for i, (_, motors) in enumerate(driver.gain_calls) if motors and "neck_yaw" in motors]
    leg_indices = [i for i, (_, motors) in enumerate(driver.gain_calls) if motors and "leg_1_yaw" in motors]
    assert neck_indices, "the neck was never softened"
    assert min(neck_indices) > max(leg_indices)
    written = [driver.gain_calls[i][0] for i in neck_indices]
    assert all(gain is not None for gain in written), "the ramp wrote a restore-to-default"
    neck_gains = cast(list[int], written)
    assert neck_gains == sorted(neck_gains, reverse=True)


def test_sleep_glides_to_neutral(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    driver = _GainGlideDriver(pose=1900)
    robot = Palmimo(driver=cast(ServoDriver, driver))

    robot.sleep(duration=0.1)

    assert abs(driver.writes[-1]["leg_1_yaw"] - 2048) <= 1


def test_sleep_leaves_the_peripherals_open() -> None:
    """The whole point: a sleeping robot can still hear "wake up". disconnect()
    closes the mic and camera, so sleep must not be built on it."""

    class _Closeable:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    mic, camera = _Closeable(), _Closeable()
    driver = _GainGlideDriver()
    robot = Palmimo(driver=cast(ServoDriver, driver), mic=cast(Any, mic), camera=cast(Any, camera))

    robot.sleep(duration=0.1)

    assert driver.gain_calls, "sleep did not actually run"
    assert not mic.closed
    assert not camera.closed


def test_sleep_without_a_driver_is_a_no_op() -> None:
    Palmimo().sleep()  # must not raise


def test_sleep_rejects_a_non_positive_duration() -> None:
    driver = _GainGlideDriver()
    robot = Palmimo(driver=cast(ServoDriver, driver))
    with pytest.raises(ValueError, match="duration"):
        robot.sleep(duration=0)


@pytest.mark.parametrize("end_gain", [0, -1, 901])
def test_sleep_rejects_an_end_gain_outside_the_holding_range(end_gain: int) -> None:
    """Zero is torque with no holding force -- the legs fold under the body,
    which is the outcome the docstring promises against. Above the running gain
    the "ramp down" is a ramp up."""
    driver = _GainGlideDriver()
    robot = Palmimo(driver=cast(ServoDriver, driver))
    with pytest.raises(ValueError, match="end_gain"):
        robot.sleep(duration=0.1, end_gain=end_gain)


def test_sleep_holds_the_neck_where_it_is_rather_than_commanding_neutral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_park_neck drops the neck gain straight after. A goal the head cannot hold
    at that gain is a stored error, and the next gain write snaps the head up out
    of it -- so the neck's goal must stay on the pose it is already holding."""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    driver = _GainGlideDriver(pose=1700)  # head down, well away from neutral
    robot = Palmimo(driver=cast(ServoDriver, driver))

    robot.sleep(duration=0.1)

    assert driver.writes, "sleep never wrote a frame"
    for frame in driver.writes:
        # Present in every frame: omitting the key is not the same as leaving the
        # neck alone -- a driver backfills an unnamed motor with NEUTRAL.
        assert frame["neck_yaw"] == 1700
        assert frame["neck_pitch1"] == 1700


def test_sleep_releases_gesture_tuning_so_the_next_step_cannot_restore_the_gain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wave leaves Position_P_Gain raised globally and _gesture_tuned stale.
    Carried through sleep, the first step() afterwards takes the release branch
    and restores the default gain on a neck just walked down to limp."""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)

    class _WaveTunableDriver(_GainGlideDriver):
        """The wave's tuning needs IIR too, or _sync_gesture_tuning skips it."""

        def set_iir(self, enabled: bool, weight: float = 0.0, motors: list[str] | None = None) -> None:
            pass

    driver = _WaveTunableDriver()
    robot = Palmimo(driver=cast(ServoDriver, driver))
    robot.wave()
    robot.step()  # applies the wave's RAM tuning
    assert robot._gesture_tuned is not None, "the wave never tuned; the test proves nothing"

    robot.sleep(duration=0.1)

    assert robot._gesture_tuned is None, "sleep left the wave's tuning applied"
    # The release must land before the ramp, not after it: a restore-to-default
    # in the middle would hand the legs full stiffness partway down.
    restores = [i for i, (value, _) in enumerate(driver.gain_calls) if value is None]
    ramp = [i for i, (value, motors) in enumerate(driver.gain_calls) if value is not None and motors]
    assert restores and ramp
    assert max(restores) < min(ramp)


def test_sleep_parks_the_neck_even_when_a_write_fails_partway(monkeypatch: pytest.MonkeyPatch) -> None:
    """EIO mid-glide is a documented failure on this bus. It must not skip the
    soft release, nor leave the engine believing a pose never reached."""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)

    class _FailsPartway(_GainGlideDriver):
        def write_positions(self, positions: dict[str, int]) -> None:
            super().write_positions(positions)
            if len(self.writes) == 2:
                raise OSError("[Errno 5] Input/output error")

    driver = _FailsPartway(pose=1900)
    robot = Palmimo(driver=cast(ServoDriver, driver))

    with pytest.raises(OSError, match="Input/output error"):
        robot.sleep(duration=1.0)

    neck_softened = [value for value, motors in driver.gain_calls if motors and "neck_yaw" in motors]
    assert neck_softened, "the neck was left held up after the failure"
    # The engine must believe the last frame actually written, not the neutral
    # target: seeding the target would make the next step() jump the remainder.
    assert robot._engine.get_positions()["leg_1_yaw"] == driver.writes[-1]["leg_1_yaw"]


def test_sleep_still_goes_limp_without_position_sense(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the glide needs feedback. DynamixelDriver.read_positions returns {}
    whenever a batch read drops -- routine on this bus -- so a fallback that
    commanded neutral at full stiffness and skipped the ramp would turn a
    transient read failure into "snap upright and stiffen"."""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)

    class _NoSense(_GainGlideDriver):
        def read_positions(self) -> dict[str, int]:
            return {}  # what the real driver returns on a dropped sync_read

    driver = _NoSense()
    robot = Palmimo(driver=cast(ServoDriver, driver))

    robot.sleep(duration=0.1)

    leg_ramp = [value for value, motors in driver.gain_calls if motors and "leg_1_yaw" in motors]
    assert leg_ramp, "the gain was never ramped down"
    assert leg_ramp[-1] == robot_module._WAKE_START_GAIN
    neck = [i for i, (_, motors) in enumerate(driver.gain_calls) if motors and "neck_yaw" in motors]
    assert neck, "the neck was never released"
    # The neck must not be on the leg ramp: _park_neck's ladder starts at 700,
    # so ramping it to 300 first would jolt the head UP before letting it down.
    assert not any(motors and "neck_yaw" in motors and "leg_1_yaw" in motors for _, motors in driver.gain_calls)
    # No position command: there is no sensed pose to glide from, and commanding
    # neutral at whatever gain the robot currently has is a snap, not a sleep.
    assert driver.writes == []


def test_sleep_leaves_the_neck_goal_where_the_head_came_to_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    """_park_neck lets the head down onto the top plate, so the goal it held
    during the glide is now far from where the head is. wake() raises the neck
    gain BEFORE writing a position, so that stored error would come out as a
    slam."""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: None)

    class _HeadDrops(_GainGlideDriver):
        """Reports the head resting on the plate once the neck has gone limp."""

        def read_positions(self) -> dict[str, int]:
            poses = super().read_positions()
            if any(motors and "neck_yaw" in motors for _, motors in self.gain_calls):
                poses = {n: (1500 if n in ("neck_yaw", "neck_pitch1") else p) for n, p in poses.items()}
            return poses

    driver = _HeadDrops(pose=2048)
    robot = Palmimo(driver=cast(ServoDriver, driver))

    robot.sleep(duration=0.1)

    assert driver.writes[-1]["neck_yaw"] == 1500, "the neck goal was left where the head no longer is"
    # The legs keep the pose the glide landed on; the settle write must not
    # re-command them (write_positions backfills an unnamed motor with NEUTRAL).
    assert abs(driver.writes[-1]["leg_1_yaw"] - 2048) <= 1
    assert robot._engine.get_positions()["neck_yaw"] == 1500


def test_neck_gesture_pv_restores_to_driver_default() -> None:
    """PV restoration reverts to the driver's own default (e.g. 600), not a hardcoded 300."""
    driver = _GlideDriver()
    robot = Palmimo(driver=cast(ServoDriver, driver))
    robot.nod()
    robot.step()  # enter -> PV=0
    robot.stop()
    robot.step()  # exit -> restore
    assert driver.pv_calls[0][0] == 0
    assert driver.pv_calls[-1][0] == 600


def test_gesture_tuning_flag_resets_on_reconnect() -> None:
    """After reconnecting, PV tuning tracking is cleared and reapplied on the next gesture."""
    driver = _GlideDriver()
    robot = Palmimo(driver=cast(ServoDriver, driver), auto_wake=False)  # wake glide is not under test here
    robot.nod()
    robot.step()  # PV=0 applied (flag set to True)
    robot.disconnect()
    robot.connect()  # connect is expected to rewrite the default PV
    n_before = len(driver.pv_calls)
    robot.nod()
    robot.step()
    assert len(driver.pv_calls) > n_before  # not skipped due to a stale flag
    assert driver.pv_calls[-1][0] == 0


def test_bow_completes_at_custom_fps() -> None:
    """Even with Palmimo(fps=30), BOW completes at its nominal duration (in seconds), legs return to
    neutral and the neck returns to its rest center."""
    robot = Palmimo(fps=30)
    robot.bow()
    e = robot.engine
    total = e.bow_enter_s + e.bow_hold_s + e.bow_exit_s
    robot.step_n(round((total + 1.0) * 30))
    pos = robot.positions
    assert pos["neck_pitch1"] == e.neck_pitch_center()
    assert all(abs(t - e.NEUTRAL) <= 2 for k, t in pos.items() if k != "neck_pitch1")


def test_play_fps_override_restores_engine_dt() -> None:
    """play(fps=...) swaps engine.dt only during playback and restores it afterward."""
    robot = Palmimo()  # fps=60
    baseline = robot.engine.dt
    list(robot.play([RoutineStep(Motion.BOW, seconds=0.2)], fps=30))
    assert robot.engine.dt == baseline


# ----------------------------------------------------------------------
# FaceDisplay bundling (Palmimo owns and exposes display symmetrically to driver)
# ----------------------------------------------------------------------


class RecordingDriver(ServoDriver):
    """Driver double that records the connect/disconnect order. Used to verify rollback for both display and speaker."""

    def __init__(self) -> None:
        self._connected = False
        self.events: list[str] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True
        self.events.append("connect")

    def disconnect(self) -> None:
        self._connected = False
        self.events.append("disconnect")

    def write_positions(self, positions: dict[str, int]) -> None:
        pass


class FakeFace:
    """FaceDisplay-compatible duck type. Records connect/wake/idle/disconnect/set_expression."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.connected = False

    def connect(self) -> None:
        self.connected = True
        self.calls.append(("connect", ()))

    def wake(self) -> str:
        self.calls.append(("wake", ()))
        return "OK WAKE"

    def idle(self) -> str:
        self.calls.append(("idle", ()))
        return "OK IDLE"

    def disconnect(self) -> None:
        self.connected = False
        self.calls.append(("disconnect", ()))

    def set_expression(self, name: str, hold_ms: int = 0) -> str:
        self.calls.append(("set_expression", (name, hold_ms)))
        return f"OK {name.upper()}"


def test_display_property_exposes_attached_face() -> None:
    """The display property gives access to the injected face (symmetric with driver)."""
    face = FakeFace()
    assert Palmimo(display=cast(FaceDisplay, face)).display is face
    assert Palmimo().display is None


def test_connect_with_display_opens_and_wakes() -> None:
    """connect() connects then wakes the display (works standalone even with driver=None)."""
    face = FakeFace()
    Palmimo(display=cast(FaceDisplay, face)).connect()
    assert [n for n, _ in face.calls] == ["connect", "wake"]
    assert face.connected is True


def test_disconnect_with_display_idles_and_closes() -> None:
    """disconnect() idles then disconnects the display (closes with a neutral face)."""
    face = FakeFace()
    robot = Palmimo(display=cast(FaceDisplay, face))
    robot.connect()
    robot.disconnect()
    assert [n for n, _ in face.calls] == ["connect", "wake", "idle", "disconnect"]
    assert face.connected is False


def test_display_only_context_manager_runs_full_ceremony() -> None:
    """with Palmimo(display=): enter does connect+wake, exit does idle+disconnect."""
    face = FakeFace()
    with Palmimo(display=cast(FaceDisplay, face)):
        pass
    assert [n for n, _ in face.calls] == ["connect", "wake", "idle", "disconnect"]


def test_connect_auto_wake_noops_without_driver() -> None:
    """auto_wake (the default) triggers Palmimo.wake(), which itself no-ops without a driver — a
    display-only (or otherwise driver-less) facade's connect() must succeed without error."""
    face = FakeFace()
    robot = Palmimo(display=cast(FaceDisplay, face))  # auto_wake=True (default), no driver attached
    robot.connect()
    assert robot.is_connected is False  # no driver to connect
    assert face.connected is True


def test_disconnect_swallows_display_errors() -> None:
    """A display I/O failure doesn't take down the whole disconnect (best-effort)."""

    class FlakyFace(FakeFace):
        def idle(self) -> str:
            self.calls.append(("idle", ()))
            raise RuntimeError("USB unplugged")

        def disconnect(self) -> None:
            self.calls.append(("disconnect", ()))
            raise RuntimeError("USB unplugged")

    face = FlakyFace()
    robot = Palmimo(display=cast(FaceDisplay, face))
    robot.connect()
    robot.disconnect()  # the exception doesn't escape
    names = [n for n, _ in face.calls]
    assert "idle" in names and "disconnect" in names


def test_set_expression_delegates_to_display() -> None:
    """set_expression calls display.set_expression(name, hold_ms) and returns its reply."""
    face = FakeFace()
    reply = Palmimo(display=cast(FaceDisplay, face)).set_expression("happy", hold_ms=3000)
    assert ("set_expression", ("happy", 3000)) in face.calls
    assert reply == "OK HAPPY"


def test_set_expression_without_display_is_noop() -> None:
    """Without a display attached, set_expression is a no-op that returns None (same shape as set_p_gain)."""
    assert Palmimo().set_expression("happy") is None


def test_connect_rolls_back_driver_when_display_fails() -> None:
    """When display startup fails, the already-opened driver is closed before re-raising
    (prevents a partial-failure leak).

    On the ``with`` path, __exit__ never runs if __enter__ raises, so connect()
    must roll back internally or the driver is left open.
    """

    class BoomFace(FakeFace):
        def wake(self) -> str:
            self.calls.append(("wake", ()))
            raise RuntimeError("serial boom")

    driver = RecordingDriver()
    robot = Palmimo(driver=cast(ServoDriver, driver), display=cast(FaceDisplay, BoomFace()))
    with pytest.raises(RuntimeError, match="serial boom"):
        robot.connect()
    # the driver goes connect -> (rolled back to) disconnect, never left open.
    assert driver.events == ["connect", "disconnect"]
    assert driver.is_connected is False


def test_connect_rolls_back_driver_when_display_connect_times_out() -> None:
    """On a dev machine with no robot attached, the driver connects fine but the
    face display's serial probe never responds. connect() must give up (rather than hang) and
    roll back the already-opened driver, same as any other connect failure."""
    never_return = threading.Event()  # never set -> the serial open blocks "forever"

    def hanging_serial_factory(port: str, baudrate: int, timeout: float = 1.0) -> Any:
        never_return.wait()
        raise AssertionError("unreachable within the test's lifetime")  # pragma: no cover

    driver = RecordingDriver()
    display = FaceDisplay(port="COM_HANG", serial_factory=hanging_serial_factory, connect_timeout=0.05)
    robot = Palmimo(driver=driver, display=display)

    start = time.perf_counter()
    with pytest.raises(FaceDisplayConnectTimeoutError, match="COM_HANG"):
        robot.connect()
    elapsed = time.perf_counter() - start

    assert elapsed < 2.0, "connect() should fail fast, not hang"
    # the driver goes connect -> (rolled back to) disconnect, never left open.
    assert driver.events == ["connect", "disconnect"]
    assert driver.is_connected is False
    assert not display.is_connected


# ----------------------------------------------------------------------
# Speaker bundling (Palmimo owns and exposes speaker symmetrically to driver/display)
# ----------------------------------------------------------------------


class FakeSpeaker:
    """Speaker-compatible duck type. Records open/say/close calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.opened = False

    def open(self) -> None:
        self.opened = True
        self.calls.append(("open", ()))

    def say(self, text: str, lang: str | None = None) -> str:
        self.calls.append(("say", (text, lang)))
        return "OK"

    def stop(self) -> None:
        self.calls.append(("stop", ()))

    def close(self) -> None:
        self.opened = False
        self.calls.append(("close", ()))


def test_speaker_property_exposes_attached_speaker() -> None:
    """The speaker property gives access to the injected speaker (symmetric with driver/display)."""
    spk = FakeSpeaker()
    assert Palmimo(speaker=cast(Speaker, spk)).speaker is spk
    assert Palmimo().speaker is None


def test_has_connectable_resource_reflects_attachments() -> None:
    """has_connectable_resource is True if either display or speaker is attached, False if all are None."""
    assert not Palmimo().has_connectable_resource  # compute-only
    assert Palmimo(display=cast(FaceDisplay, FakeFace())).has_connectable_resource
    assert Palmimo(speaker=cast(Speaker, FakeSpeaker())).has_connectable_resource


def test_connect_opens_speaker() -> None:
    """connect() calls speaker.open() (works standalone even with driver=None)."""
    spk = FakeSpeaker()
    Palmimo(speaker=cast(Speaker, spk)).connect()
    assert [n for n, _ in spk.calls] == ["open"]
    assert spk.opened is True


def test_disconnect_closes_speaker() -> None:
    """disconnect() calls speaker.close()."""
    spk = FakeSpeaker()
    robot = Palmimo(speaker=cast(Speaker, spk))
    robot.connect()
    robot.disconnect()
    assert [n for n, _ in spk.calls] == ["open", "close"]
    assert spk.opened is False


def test_speaker_only_context_manager_runs_full_ceremony() -> None:
    """with Palmimo(speaker=): enter does open, exit does close."""
    spk = FakeSpeaker()
    with Palmimo(speaker=cast(Speaker, spk)):
        pass
    assert [n for n, _ in spk.calls] == ["open", "close"]


def test_disconnect_swallows_speaker_errors() -> None:
    """A speaker.close() failure doesn't take down the whole disconnect (best-effort)."""

    class FlakySpeaker(FakeSpeaker):
        def close(self) -> None:
            self.calls.append(("close", ()))
            raise RuntimeError("device hang")

    spk = FlakySpeaker()
    robot = Palmimo(speaker=cast(Speaker, spk))
    robot.connect()
    robot.disconnect()  # the exception doesn't escape
    assert "close" in [n for n, _ in spk.calls]


def test_say_delegates_to_speaker() -> None:
    """say calls speaker.say(text, lang)."""
    spk = FakeSpeaker()
    Palmimo(speaker=cast(Speaker, spk)).say("こんにちは", lang="ja")
    assert ("say", ("こんにちは", "ja")) in spk.calls


def test_say_without_speaker_is_noop() -> None:
    """Without a speaker attached, say is a no-op that returns None (same shape as set_expression)."""
    assert Palmimo().say("hello") is None


def test_stop_speech_delegates_to_speaker() -> None:
    """stop_speech() calls speaker.stop() (barge-in support)."""
    spk = FakeSpeaker()
    Palmimo(speaker=cast(Speaker, spk)).stop_speech()
    assert ("stop", ()) in spk.calls


def test_stop_speech_without_speaker_is_noop() -> None:
    """Without a speaker attached, stop_speech() is a safe no-op."""
    Palmimo().stop_speech()  # must not raise


def test_connect_rolls_back_driver_when_speaker_open_fails() -> None:
    """When speaker.open() fails, the already-opened driver is closed before re-raising
    (prevents a partial-failure leak)."""

    class BoomSpeaker(FakeSpeaker):
        def open(self) -> None:
            self.calls.append(("open", ()))
            raise RuntimeError("piper boom")

    driver = RecordingDriver()
    robot = Palmimo(driver=cast(ServoDriver, driver), speaker=cast(Speaker, BoomSpeaker()))
    with pytest.raises(RuntimeError, match="piper boom"):
        robot.connect()
    # speaker.open() fails last -> the driver goes connect -> rolled back to disconnect.
    assert driver.events == ["connect", "disconnect"]
    assert driver.is_connected is False


# ----------------------------------------------------------------------
# HeadCamera bundling (Palmimo owns and exposes camera symmetrically to driver/display/speaker)
# ----------------------------------------------------------------------


class FakeCamera:
    """HeadCamera-compatible duck type. Records open/start_drain/close calls.

    Like the real thing, close() folds in stopping the drain (there's no separate
    stop_drain contract to call).
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.is_open = False

    def open(self) -> None:
        self.is_open = True
        self.calls.append("open")

    def start_drain(self) -> None:
        self.calls.append("start_drain")

    def close(self) -> None:
        self.is_open = False
        self.calls.append("close")


def test_camera_property_exposes_attached_camera() -> None:
    """The camera property gives access to the injected camera, and alone makes
    has_connectable_resource True (symmetric with driver/display/speaker)."""
    cam = FakeCamera()
    robot = Palmimo(camera=cast(HeadCamera, cam))
    assert robot.camera is cam
    assert robot.has_connectable_resource
    assert Palmimo().camera is None


def test_connect_opens_camera_and_starts_drain() -> None:
    """connect() calls camera.open() then start_drain(), in order (works standalone even with driver=None).

    Same shape as display's connect+wake: the facade's connect() bundles each
    peripheral's own startup ceremony.
    """
    cam = FakeCamera()
    Palmimo(camera=cast(HeadCamera, cam)).connect()
    assert cam.calls == ["open", "start_drain"]
    assert cam.is_open is True


def test_disconnect_closes_camera() -> None:
    """disconnect() calls camera.close() (which folds in stopping the drain)."""
    cam = FakeCamera()
    robot = Palmimo(camera=cast(HeadCamera, cam))
    robot.connect()
    robot.disconnect()
    assert cam.calls == ["open", "start_drain", "close"]
    assert cam.is_open is False


def test_camera_only_context_manager_runs_full_ceremony() -> None:
    """with Palmimo(camera=): enter does open+start_drain, exit does close."""
    cam = FakeCamera()
    with Palmimo(camera=cast(HeadCamera, cam)):
        pass
    assert cam.calls == ["open", "start_drain", "close"]


def test_disconnect_swallows_camera_errors() -> None:
    """A camera.close() failure doesn't take down the whole disconnect (best-effort)."""

    class FlakyCamera(FakeCamera):
        def close(self) -> None:
            self.calls.append("close")
            raise RuntimeError("USB unplugged")

    cam = FlakyCamera()
    robot = Palmimo(camera=cast(HeadCamera, cam))
    robot.connect()
    robot.disconnect()  # the exception doesn't escape
    assert "close" in cam.calls


def test_connect_rolls_back_driver_when_camera_open_fails() -> None:
    """When camera.open() fails, the already-opened driver is closed before re-raising
    (prevents a partial-failure leak).

    An open failure must not vanish into a background thread's logs — it has to
    reach the caller as an exception from connect() (same treatment as driver/display/speaker).
    """

    class BoomCamera(FakeCamera):
        def open(self) -> None:
            self.calls.append("open")
            raise RuntimeError("cannot open camera 0")

    driver = RecordingDriver()
    robot = Palmimo(driver=cast(ServoDriver, driver), camera=cast(HeadCamera, BoomCamera()))
    with pytest.raises(RuntimeError, match="cannot open camera 0"):
        robot.connect()
    # camera.open() fails last -> the driver goes connect -> rolled back to disconnect.
    assert driver.events == ["connect", "disconnect"]
    assert driver.is_connected is False


class FakeMic:
    """Microphone-compatible duck type. Records open/close calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.is_open = False

    def open(self) -> None:
        self.is_open = True
        self.calls.append("open")

    def close(self) -> None:
        self.is_open = False
        self.calls.append("close")


def test_mic_property_exposes_attached_mic() -> None:
    """The mic property gives access to the injected mic, and alone makes
    has_connectable_resource True (symmetric with driver/display/speaker/camera)."""
    mic = FakeMic()
    robot = Palmimo(mic=cast(Microphone, mic))
    assert robot.mic is mic
    assert robot.has_connectable_resource
    assert Palmimo().mic is None


def test_mic_only_context_manager_runs_full_ceremony() -> None:
    """with Palmimo(mic=): enter does open (a connectivity probe), exit does close."""
    mic = FakeMic()
    with Palmimo(mic=cast(Microphone, mic)):
        assert mic.calls == ["open"]
    assert mic.calls == ["open", "close"]
    assert mic.is_open is False


def test_disconnect_swallows_mic_errors() -> None:
    """A mic.close() failure doesn't take down the whole disconnect (best-effort)."""

    class FlakyMic(FakeMic):
        def close(self) -> None:
            self.calls.append("close")
            raise RuntimeError("USB unplugged")

    mic = FlakyMic()
    driver = RecordingDriver()
    robot = Palmimo(driver=cast(ServoDriver, driver), mic=cast(Microphone, mic))
    robot.connect()
    robot.disconnect()  # the exception doesn't escape, and the driver still gets disconnected
    assert "close" in mic.calls
    assert driver.is_connected is False


def test_connect_rolls_back_others_when_mic_open_fails() -> None:
    """When mic.open() (a connectivity probe) fails, the already-opened camera / driver are closed before re-raising."""

    class BoomMic(FakeMic):
        def open(self) -> None:
            self.calls.append("open")
            raise RuntimeError("cannot access microphone")

    driver = RecordingDriver()
    cam = FakeCamera()
    robot = Palmimo(driver=cast(ServoDriver, driver), camera=cast(HeadCamera, cam), mic=cast(Microphone, BoomMic()))
    with pytest.raises(RuntimeError, match="cannot access microphone"):
        robot.connect()
    # mic is opened last -> camera and driver are closed via rollback.
    assert cam.calls == ["open", "start_drain", "close"]
    assert driver.events == ["connect", "disconnect"]


def test_mic_stream_connect_and_close_drive_open_and_close() -> None:
    """connect/disconnect drive open/close even when mic is a MicStream instance.

    MicStream satisfies the same open()/close()/is_open contract as Microphone,
    so the facade needs no logic change — verified with a fake stream factory
    (no real device / sounddevice required)."""
    from palmimo_sdk.io import MicStream, _mic_registry

    class _FakeInputStream:
        def __init__(self, samplerate: int, channels: int, dtype: str, blocksize: int, device: int | None) -> None:
            self.started = False
            self.stopped = False
            self.closed = False

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

        def close(self) -> None:
            self.closed = True

        def read(self, n: int) -> tuple[Any, bool]:
            import numpy as np

            return np.zeros((n, 1), dtype=np.int16), False

    def factory(samplerate: int, channels: int, dtype: str, blocksize: int, device: int | None) -> _FakeInputStream:
        return _FakeInputStream(samplerate, channels, dtype, blocksize, device)

    # processors=[]: this test only cares about open/close lifecycle wiring,
    # not audio processing — MicStream's real default (a Denoiser) would need
    # the `voice` extra / a downloaded model, unrelated to what's under test.
    mic_stream = MicStream(input_stream_factory=factory, device_key="robot-test-mic", processors=[])
    robot = Palmimo(mic=mic_stream)
    assert robot.mic is mic_stream
    try:
        with robot:
            assert mic_stream.is_open
        assert not mic_stream.is_open
    finally:
        # Safety net: don't leak the registry entry into other tests if an
        # assertion above fails mid-test.
        _mic_registry.unregister("robot-test-mic", mic_stream)
