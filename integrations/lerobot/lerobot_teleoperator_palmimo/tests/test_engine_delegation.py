"""Regression guard on what PalmimoTeleop delegates and what it maps.

Leg motion belongs to palmimo_sdk's MotionEngine, and this teleop only decides
which Motion the held keys mean and where the neck goes. These tests hold that
split: they check the Motion that got selected, not the leg angles behind it.

lerobot is imported, but the keyboard listener (pynput) never starts — connect()
is not called, and held-key sets are handed straight to the _update_*_from_keys
methods. That keeps the suite runnable without a display or a robot.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

import pytest

from palmimo_sdk import Motion


if TYPE_CHECKING:
    from lerobot_teleoperator_palmimo.palmimo import PalmimoTeleop


pytest.importorskip("lerobot")


def _teleop() -> PalmimoTeleop:
    from lerobot_teleoperator_palmimo.config_palmimo import PalmimoTeleopConfig
    from lerobot_teleoperator_palmimo.palmimo import PalmimoTeleop

    return PalmimoTeleop(PalmimoTeleopConfig())


def test_move_commands_select_engine_motion() -> None:
    """Each command selects a Motion; the direction signs stay in the engine."""
    t = _teleop()
    cases = [
        (t._move_forward, Motion.FORWARD),
        (t._move_backward, Motion.BACKWARD),
        (t._move_left, Motion.STRAFE_LEFT),
        (t._move_right, Motion.STRAFE_RIGHT),
        (t._rotate_left, Motion.ROTATE_LEFT),
        (t._rotate_right, Motion.ROTATE_RIGHT),
        (t._do_dance, Motion.DANCE),
    ]
    for command, expected in cases:
        command()
        assert t._engine.motion is expected
        assert t._engine_driven is True


def test_stop_sets_engine_idle() -> None:
    """_stop_motion idles the engine and leaves the frame engine-driven."""
    t = _teleop()
    t._move_forward()
    t._stop_motion()
    assert t._engine.motion is Motion.IDLE
    assert t._engine_driven is True


def test_engine_step_feeds_full_motor_set() -> None:
    """The engine returns all 20 motors; the teleop keeps only the 18 leg ones."""
    t = _teleop()
    t._move_forward()
    pos = t._engine.step()
    assert len(pos) == 20
    assert {"leg_1_yaw", "leg_6_pitch2", "neck_yaw"} <= set(pos)
    assert len(t._leg_positions) == 18


def test_held_char_keys_map_to_engine_motion() -> None:
    """A held-key set, in the shape KeyboardTeleop reports it, picks the right Motion."""
    t = _teleop()
    keys = t.config.teleop_keys
    cases = [
        (keys["forward"], Motion.FORWARD),
        (keys["backward"], Motion.BACKWARD),
        (keys["strafe_left"], Motion.STRAFE_LEFT),
        (keys["strafe_right"], Motion.STRAFE_RIGHT),
        (keys["rotate_left"], Motion.ROTATE_LEFT),
        (keys["rotate_right"], Motion.ROTATE_RIGHT),
        (keys["dance"], Motion.DANCE),
    ]
    for char, expected in cases:
        t._update_motion_from_keys({char})
        assert t._engine.motion is expected
        assert t._engine_driven is True


def test_no_keys_idles_engine() -> None:
    """Releasing every key idles the engine, which then eases back to neutral."""
    t = _teleop()
    t._update_motion_from_keys({t.config.teleop_keys["forward"]})
    t._update_motion_from_keys(set())
    assert t._engine.motion is Motion.IDLE
    assert t._engine_driven is True


def test_neck_char_keys_move_neck() -> None:
    """The neck keys, remapped off the arrow keys, drive their axis away from center."""
    keys = _teleop().config.teleop_keys
    center = _teleop()._neutral

    t = _teleop()
    t._update_neck_from_keys({keys["neck_pitch_up"]})
    assert t._neck_positions["neck_pitch1"] > center

    t = _teleop()
    t._update_neck_from_keys({keys["neck_pitch_down"]})
    assert t._neck_positions["neck_pitch1"] < center

    t = _teleop()
    t._update_neck_from_keys({keys["neck_yaw_left"]})
    assert t._neck_positions["neck_yaw"] > center

    t = _teleop()
    t._update_neck_from_keys({keys["neck_yaw_right"]})
    assert t._neck_positions["neck_yaw"] < center


class _FakeKeyboard:
    """Mimics lerobot KeyboardTeleop.get_action(): returns ``dict.fromkeys(held, None)``
    — keys are the held chars, values are ``None``. Guards the extraction path so a
    well-meaning ``{k for k, v in ... if v}`` rewrite (which would drop every key,
    since values are falsy) is caught."""

    def __init__(self, held: Iterable[str]) -> None:
        self._held = set(held)
        self.is_connected = True

    def get_action(self) -> dict[str, None]:
        return dict.fromkeys(self._held, None)

    def disconnect(self) -> None:
        self.is_connected = False


def test_get_action_extracts_held_keys_from_keyboard() -> None:
    """get_action() pulls the held keys out of the keyboard and acts on them."""
    t = _teleop()
    t._keyboard = _FakeKeyboard({t.config.teleop_keys["forward"]})
    t._is_connected = True

    action = t.get_action()
    assert t._engine.motion is Motion.FORWARD
    assert "leg_1_yaw.pos" in action and "neck_yaw.pos" in action

    # With nothing held the engine idles. Paired with the assertion above, an
    # extraction that always came back empty could not satisfy both.
    t._keyboard = _FakeKeyboard(set())
    t.get_action()
    assert t._engine.motion is Motion.IDLE
