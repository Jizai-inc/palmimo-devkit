"""Tuning knobs reject out-of-range values with ValueError, on the engine and on the facade."""

import math

import pytest

from palmimo_sdk import MotionEngine, Palmimo


_RATE = MotionEngine._MIN_RATE
_GAP = MotionEngine._CLAP_GAP_FLOOR
_REST = MotionEngine._NECK_REST_LIMIT_DEG

# knob -> (lowest, highest, lowest is exclusive); None = unbounded.
FLOAT_KNOB_RANGES: dict[str, tuple[float | None, float | None, bool]] = {
    **dict.fromkeys(
        (
            "wave_speed",
            "wave_intro_speed",
            "wave_raise_speed",
            "wave_both_intro_speed",
            "wave_both_speed",
            "clap_intro_speed",
        ),
        (_RATE, None, False),
    ),
    **dict.fromkeys(("wave_decay", "wave_both_decay", "clap_decay", "nod_decay", "shake_decay"), (0.0, 1.0, False)),
    **dict.fromkeys(("wave_period", "nod_period", "shake_period"), (0.05, None, False)),
    "clap_period": (0.1, None, False),
    "wave_dwell": (0.0, 0.95, False),
    "clap_dwell": (0.0, 0.8, False),
    "dance_dwell": (0.0, 0.99, False),
    "wave_both_phase": (0.0, 1.0, False),
    **dict.fromkeys(
        ("wave_both_yaw", "wave_size", "wave_both_size", "nod_amp_deg", "shake_amp_deg"), (0.0, None, False)
    ),
    **dict.fromkeys(("wave_both_lean", "wave_both_noseup", "wave_both_mid_forward"), (None, None, False)),
    "clap_gap": (_GAP, None, False),
    "clap_open": (_GAP, None, False),
    "clap_height": (MotionEngine._CLAP_HEIGHT_FLOOR, None, False),
    "nod_first_down_scale": (0.1, None, False),
    "neck_rest_pitch_deg": (-_REST, _REST, False),
    "dance_roll_deg": (0.0, MotionEngine.DANCE_ROLL_MAX_DEG, False),
    "dance_pivot_h": (0.0, None, True),
    "dance_head_h": (0.0, None, True),
    "dance_speed": (0.0, 0.125, True),
}
COUNT_KNOBS = ("wave_count", "clap_count", "nod_count", "shake_swings")


def _just_outside() -> list[tuple[str, float]]:
    """One rejected value past each finite bound (an exclusive low bound is itself rejected)."""
    out: list[tuple[str, float]] = []
    for knob, (lo, hi, lo_open) in FLOAT_KNOB_RANGES.items():
        if lo is not None:
            out.append((knob, lo if lo_open else lo - 0.001))
        if hi is not None:
            out.append((knob, hi + 0.001))
    return out


def _accepted_bounds() -> list[tuple[str, float]]:
    """Every bound the range includes."""
    return [
        (knob, bound)
        for knob, (lo, hi, lo_open) in FLOAT_KNOB_RANGES.items()
        for bound in (None if lo_open else lo, hi)
        if bound is not None
    ]


@pytest.mark.parametrize(("knob", "value"), _just_outside())
def test_knob_past_its_bound_raises_and_keeps_the_old_value(knob: str, value: float) -> None:
    engine = MotionEngine()
    before = getattr(engine, knob)
    with pytest.raises(ValueError, match=knob):
        setattr(engine, knob, value)
    assert getattr(engine, knob) == before


@pytest.mark.parametrize(("knob", "value"), _accepted_bounds())
def test_knob_accepts_its_inclusive_bound(knob: str, value: float) -> None:
    engine = MotionEngine()
    setattr(engine, knob, value)
    assert getattr(engine, knob) == value


@pytest.mark.parametrize("knob", FLOAT_KNOB_RANGES)
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_float_knob_rejects_non_finite_values(knob: str, value: float) -> None:
    with pytest.raises(ValueError, match=knob):
        setattr(MotionEngine(), knob, value)


@pytest.mark.parametrize("knob", COUNT_KNOBS)
@pytest.mark.parametrize("value", [0, -1, 2.5, True, math.nan, math.inf])
def test_count_knob_rejects_non_positive_or_non_integer_values(knob: str, value: float) -> None:
    engine = MotionEngine()
    before = getattr(engine, knob)
    with pytest.raises(ValueError, match=knob):
        setattr(engine, knob, value)
    assert getattr(engine, knob) == before


def test_count_knob_accepts_an_integral_float() -> None:
    engine = MotionEngine()
    engine.wave_count = 3.0
    assert engine.wave_count == 3


@pytest.mark.parametrize(("knob", "value"), [("dance_roll_deg", 30), ("wave_count", 2.5)])
def test_facade_setter_raises_and_leaves_the_old_value(knob: str, value: float) -> None:
    robot = Palmimo()
    before = getattr(robot, knob)
    with pytest.raises(ValueError, match=knob):
        setattr(robot, knob, value)
    assert getattr(robot, knob) == before
