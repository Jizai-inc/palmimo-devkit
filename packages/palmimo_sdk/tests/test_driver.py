"""ServoDriver contract: lifecycle, position streaming, and the with-block.

Uses an in-memory recording driver so the contract is verified without any
serial hardware. The recorder mirrors the ``{motor}.pos`` payload shape a
serial driver will send to the bus, so that contract is checked too.
"""

from typing import Any

import pytest

from palmimo_sdk import Palmimo, ServoDriver


class RecordingDriver(ServoDriver):
    """A ServoDriver that records connect/disconnect events and writes."""

    def __init__(self) -> None:
        self._connected = False
        self.events: list[str] = []
        self.writes: list[dict[str, int]] = []

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
        # Mirror the {motor}.pos payload shape a serial driver sends to the bus.
        action = {f"{motor}.pos": int(tick) for motor, tick in positions.items()}
        self.writes.append(action)


def test_connect_without_any_resource_raises() -> None:
    """connect() raises RuntimeError when none of driver / display / speaker / camera / mic is injected."""
    with pytest.raises(RuntimeError, match="no driver, display, speaker, camera, or mic"):
        Palmimo().connect()


def test_with_block_manages_lifecycle() -> None:
    """with: connect on enter, return-to-neutral then disconnect on exit."""
    driver = RecordingDriver()
    with Palmimo(driver=driver, auto_wake=False) as robot:
        assert robot.is_connected
        robot.forward()
        robot.step_n(20)
    assert not robot.is_connected
    assert driver.events[0] == "connect"
    assert driver.events[-1] == "disconnect"


def test_step_streams_positions_to_driver() -> None:
    """While the driver is connected, every step() writes positions to the driver."""
    driver = RecordingDriver()
    with Palmimo(driver=driver, auto_wake=False) as robot:
        robot.forward()
        robot.step_n(20)
    # 20 forward steps, plus the idle steps return_to_neutral streams on exit
    # (converges in <=max_steps, so the exact count isn't pinned).
    assert len(driver.writes) > 20


def test_payload_uses_pos_suffix() -> None:
    """The payload passed to the driver has every key suffixed with '.pos'."""
    driver = RecordingDriver()
    with Palmimo(driver=driver, auto_wake=False) as robot:
        robot.forward()
        robot.step()
    payload = driver.writes[0]
    assert len(payload) == 20
    assert all(key.endswith(".pos") for key in payload)


def test_exit_eases_servos_back_to_neutral() -> None:
    """The return-to-neutral on exit leaves the final frame near neutral (neck pitch settles at the rest-trim position)."""
    driver = RecordingDriver()
    with Palmimo(driver=driver, auto_wake=False) as robot:
        robot.forward()
        robot.step_n(20)
        rest = robot.engine.neck_pitch_center()
    last = driver.writes[-1]
    assert abs(last["neck_pitch1.pos"] - rest) < 5  # the neck's "forward" includes the rest trim
    assert all(abs(tick - 2048) < 5 for name, tick in last.items() if name != "neck_pitch1.pos")


def test_exception_in_block_skips_neutral_but_disconnects() -> None:
    """An exception inside the with block: return-to-neutral is skipped, but disconnect still runs."""
    driver = RecordingDriver()
    with pytest.raises(RuntimeError, match="boom"), Palmimo(driver=driver, auto_wake=False) as robot:
        robot.forward()
        robot.step_n(5)
        writes_before = len(driver.writes)
        raise RuntimeError("boom")
    # on exception, return_to_neutral's idle streaming never runs (write count doesn't grow)
    assert len(driver.writes) == writes_before
    assert driver.events[-1] == "disconnect"
    assert not robot.is_connected


def test_disconnect_is_idempotent_and_safe_when_idle() -> None:
    """disconnect is safe when not connected and raises nothing when called multiple times."""
    driver = RecordingDriver()
    robot = Palmimo(driver=driver, auto_wake=False)
    robot.disconnect()
    robot.connect()
    robot.disconnect()
    robot.disconnect()
    assert not robot.is_connected


def test_compute_only_step_returns_positions_without_driver() -> None:
    """Without a driver (compute-only): step() returns a position dict and the connection state is False."""
    robot = Palmimo()
    robot.forward()
    pos = robot.step()
    assert len(pos) == 20
    assert not robot.is_connected


class TuningRecordingDriver(RecordingDriver):
    """A driver that also records the per-axis RAM tweaks the wave gesture uses."""

    def __init__(self) -> None:
        super().__init__()
        self.tuning: list[tuple[Any, ...]] = []
        # Not part of ServoDriver's contract -- a test-only knob some tests
        # set directly (mirroring a driver-configured default PV) to verify
        # release restores that value rather than a hardcoded one.
        self.profile_velocity: int | None = None

    def set_profile_velocity_units(self, value: int, motors: list[str] | None = None) -> None:
        self.tuning.append(("pv", value, tuple(motors) if motors else None))

    def set_position_p_gain(self, value: int | None, motors: list[str] | None = None) -> None:
        self.tuning.append(("gain", value, tuple(motors) if motors else None))

    def set_iir(self, enabled: bool, alpha: float | None = None, motors: list[str] | None = None) -> None:
        self.tuning.append(("iir", enabled, alpha, tuple(motors) if motors else None))

    def read_positions(self) -> dict[str, int]:
        return {}  # no position sense — timed glides degrade to a single write


class DanceRecordingDriver(TuningRecordingDriver):
    """A tuning-recorder that can also report a present pose (for the dance end-hold)."""

    def __init__(self) -> None:
        super().__init__()
        self.present: dict[str, int] = {}

    def read_positions(self) -> dict[str, int]:
        return dict(self.present)


def test_perform_dance_end_hold_softens_only_legs_then_restores() -> None:
    """end-hold holds the legs at their actual position with low gain, leaves the neck untouched, and restores gain when it ends."""
    driver = DanceRecordingDriver()
    robot = Palmimo(driver=driver, auto_wake=False)
    robot.connect()
    # Servos sitting at neutral when the lingering hold begins.
    driver.present = dict.fromkeys(robot.engine.get_positions(), 2048)
    robot.perform_dance(sways=0, end_hold=0.01, settle=0.0)

    gain_calls = [t for t in driver.tuning if t[0] == "gain"]
    soft = [t for t in gain_calls if t[1] == 300]
    assert soft, "end-hold should lower the leg P-gain to 300"
    motors = set(soft[0][2])
    # Legs only — the neck is excluded so the head keeps full gain (no droop).
    assert "neck_pitch1" not in motors and "neck_yaw" not in motors
    assert "leg_1_pitch1" in motors and len(motors) == 18
    # Restored to the default afterwards (None = firmware default, all motors).
    assert ("gain", None, None) in driver.tuning
    # The hold write commands only the legs; the neck is backfilled to neutral by
    # the driver, keeping the head up.
    hold_write = driver.writes[0]
    assert "neck_pitch1.pos" not in hold_write
    assert "leg_1_pitch1.pos" in hold_write


def test_wave_applies_then_restores_per_axis_tuning() -> None:
    """During wave, arm PV=0 / IIR=0.1 / gain=1300 are applied automatically and restored on stop."""
    driver = TuningRecordingDriver()
    robot = Palmimo(driver=driver, auto_wake=False)
    robot.connect()
    robot.set_motion("wave_left")  # leg 3
    robot.step()
    arm = ("leg_3_yaw", "leg_3_pitch1", "leg_3_pitch2")
    assert ("pv", 0, arm) in driver.tuning  # arm crisp
    assert ("gain", 1300, None) in driver.tuning  # tighten all
    assert ("iir", True, 0.1, arm) in driver.tuning  # soften arm
    # Steady-state: no redundant writes once applied.
    driver.tuning.clear()
    robot.step_n(3)
    assert driver.tuning == []
    # Stop restores defaults (gain None, iir off).
    robot.stop()
    robot.step()
    assert ("iir", False, None, None) in driver.tuning
    assert ("gain", None, None) in driver.tuning


def test_wave_switch_legs_retunes_the_new_arm() -> None:
    """wave_left → wave_right releases the old arm's tuning and applies it to the new arm (leg6)."""
    driver = TuningRecordingDriver()
    robot = Palmimo(driver=driver, auto_wake=False)
    robot.connect()
    robot.set_motion("wave_left")
    robot.step()
    driver.tuning.clear()
    robot.set_motion("wave_right")  # leg 6
    robot.step()
    arm6 = ("leg_6_yaw", "leg_6_pitch1", "leg_6_pitch2")
    assert ("iir", False, None, None) in driver.tuning  # release old arm first
    assert ("iir", True, 0.1, arm6) in driver.tuning  # then tune the new one


@pytest.mark.parametrize("name,alpha", [("wave_both", 0.1), ("clap", 0.5)])
def test_two_hand_gestures_tune_both_arms(name: str, alpha: float) -> None:
    """wave_both / clap apply PV=0/IIR to all 6 axes of both forearms (leg3+leg6), restoring on stop.

    The IIR weight is per-gesture: wave_both uses the official wave's 0.1,
    clap uses 0.5 (measured: 0.1 damps the open/close oscillation so much the
    hands never fully close; even 0.5 still passes ~81% at the shipped 5Hz
    default, so the measured closest approach is ~31mm — see the
    _CLAP_ARM_IIR comment in robot.py).
    """
    driver = TuningRecordingDriver()
    robot = Palmimo(driver=driver, auto_wake=False)
    robot.connect()
    robot.set_motion(name)
    robot.step()
    arms = ("leg_3_yaw", "leg_3_pitch1", "leg_3_pitch2", "leg_6_yaw", "leg_6_pitch1", "leg_6_pitch2")
    assert ("pv", 0, arms) in driver.tuning
    assert ("gain", 1300, None) in driver.tuning
    assert ("iir", True, alpha, arms) in driver.tuning
    driver.tuning.clear()
    robot.stop()
    robot.step()
    assert ("iir", False, None, None) in driver.tuning
    assert ("gain", None, None) in driver.tuning


def test_wave_both_to_clap_switch_reapplies_the_arm_iir() -> None:
    """wave_both → clap re-tunes even though the arm pair is unchanged, because the IIR weight changes."""
    driver = TuningRecordingDriver()
    robot = Palmimo(driver=driver, auto_wake=False)
    robot.connect()
    robot.set_motion("wave_both")
    robot.step()
    driver.tuning.clear()
    robot.set_motion("clap")
    robot.step()
    arms = ("leg_3_yaw", "leg_3_pitch1", "leg_3_pitch2", "leg_6_yaw", "leg_6_pitch1", "leg_6_pitch2")
    assert ("iir", False, None, None) in driver.tuning  # release the wave weight
    assert ("iir", True, 0.5, arms) in driver.tuning  # apply the clap weight


def test_wave_single_to_both_retunes_the_arm_set() -> None:
    """wave_left → wave_both releases the single-arm tuning and applies it to both arms."""
    driver = TuningRecordingDriver()
    robot = Palmimo(driver=driver, auto_wake=False)
    robot.connect()
    robot.set_motion("wave_left")
    robot.step()
    driver.tuning.clear()
    robot.set_motion("wave_both")
    robot.step()
    arms = ("leg_3_yaw", "leg_3_pitch1", "leg_3_pitch2", "leg_6_yaw", "leg_6_pitch1", "leg_6_pitch2")
    assert ("iir", False, None, None) in driver.tuning  # release the single arm
    assert ("iir", True, 0.1, arms) in driver.tuning  # then tune both


def test_wave_tuning_noops_for_driver_without_ram_tweaks() -> None:
    """A driver without the RAM-tuning API passes through wave without raising."""
    driver = RecordingDriver()  # no set_profile_velocity_units / set_iir / set_position_p_gain
    robot = Palmimo(driver=driver, auto_wake=False)
    robot.connect()
    robot.set_motion("wave_left")
    robot.step_n(3)  # must not raise
    assert len(driver.writes) == 3


def test_choreography_wave_cue_keeps_left_right_side() -> None:
    """play()'s legacy cues 'wave_left'/'wave_right' set wave_leg to the correct side."""
    robot = Palmimo()  # compute-only
    gen = robot.play([("wave_left", 0.05)])
    next(gen)
    assert robot._engine.wave_leg == 3  # front-left
    gen = robot.play([("wave_right", 0.05)])
    next(gen)
    assert robot._engine.wave_leg == 6  # front-right


def test_bare_wave_always_uses_front_right() -> None:
    """robot.wave() always settles on front-right, without carrying over a prior wave_left state."""
    robot = Palmimo()  # compute-only
    robot.set_motion("wave_left")
    assert robot._engine.wave_leg == 3
    robot.wave()  # docstring promises front-right
    assert robot._engine.wave_leg == 6


def test_wave_to_stretch_switch_keeps_stretch_profile() -> None:
    """A direct WAVE → STRETCH switch still leaves stretch's PV=120 intact (clobber regression)."""
    driver = TuningRecordingDriver()
    robot = Palmimo(driver=driver, auto_wake=False)
    robot.connect()
    robot.set_motion("wave_left")
    robot.step()
    driver.tuning.clear()
    robot.set_motion("stretch")
    robot.step()
    all_axis_pv = [t for t in driver.tuning if t[0] == "pv" and t[2] is None]
    assert all_axis_pv[-1] == ("pv", 120, None)  # the last all-axis PV write is stretch's value
    # no further writes after that (the guard holds the new state)
    driver.tuning.clear()
    robot.step_n(3)
    assert driver.tuning == []


def test_timed_return_to_neutral_releases_stretch_profile() -> None:
    """stretch → return_to_neutral(duration=) releases PV back to the default."""
    driver = TuningRecordingDriver()
    robot = Palmimo(driver=driver, auto_wake=False)
    robot.connect()
    robot.set_motion("stretch")
    robot.step()
    driver.tuning.clear()
    robot.return_to_neutral(duration=0.05)
    assert ("pv", 300, None) in driver.tuning  # released before the glide starts


def test_gesture_release_restores_driver_configured_default() -> None:
    """Release restores the driver's own configured PV, not a hardcoded 300."""
    driver = TuningRecordingDriver()
    driver.profile_velocity = 500  # equivalent to a ctor setting
    robot = Palmimo(driver=driver, auto_wake=False)
    robot.connect()
    robot.set_motion("stretch")
    robot.step()
    robot.stop()
    robot.step()
    assert ("pv", 500, None) in driver.tuning


def test_read_telemetry_is_optional_and_names_the_driver_that_lacks_it() -> None:
    """A backend that cannot sense health leaves the guards nothing to judge on."""
    driver = RecordingDriver()
    with pytest.raises(NotImplementedError, match="RecordingDriver"):
        driver.read_telemetry()
