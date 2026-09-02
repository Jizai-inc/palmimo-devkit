"""Regression guard on which joints the teleop's action describes.

A teleop action is not a list of joints to move — it is the description of a
frame. `lerobot-record` builds each dataset frame from the *robot's* feature
names and reads the values out of the teleop's action, so a joint the teleop
declines to mention is not left alone: it makes the frame unbuildable and
recording dies with a KeyError. Every joint the robot exposes must therefore
appear, including the ones this teleop only ever holds at neutral.

get_action() has two branches — a neutral one before connect and the real one
after — and only the second is what recording runs through, so that is the one
these tests hold. No hardware: the keyboard is a stand-in and connect() is
never called.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest


if TYPE_CHECKING:
    from lerobot_teleoperator_palmimo.palmimo import PalmimoTeleop


pytest.importorskip("lerobot")

# The full joint set, spelled out rather than imported from the robot plugin:
# this package does not depend on it, and the point is to catch the two drifting
# apart. Mirrors lerobot_robot_palmimo.motor_layout.
ALL_JOINTS = frozenset(
    [f"leg_{leg}_{joint}.pos" for leg in range(1, 7) for joint in ("yaw", "pitch1", "pitch2")]
    + ["neck_pitch1.pos", "neck_pitch2.pos", "neck_yaw.pos"]
)


class _StandInKeyboard:
    """Stands in for lerobot's pynput-backed KeyboardTeleop."""

    def __init__(self, pressed: tuple[str, ...] = ()):
        self.is_connected = True
        self._pressed = pressed

    def get_action(self) -> dict[str, Any]:
        return dict.fromkeys(self._pressed, True)


def _teleop() -> PalmimoTeleop:
    from lerobot_teleoperator_palmimo.config_palmimo import PalmimoTeleopConfig
    from lerobot_teleoperator_palmimo.palmimo import PalmimoTeleop

    return PalmimoTeleop(PalmimoTeleopConfig())


def _connected_teleop(*pressed: str) -> PalmimoTeleop:
    """A teleop past connect(), without starting a real keyboard listener."""
    t = _teleop()
    t._keyboard = _StandInKeyboard(pressed)
    t._is_connected = True
    return t


def test_action_features_cover_every_joint_on_the_robot() -> None:
    assert set(_teleop().action_features) == ALL_JOINTS


def test_the_connected_action_covers_every_joint() -> None:
    """This is the branch lerobot-record reads; the KeyError came from here."""
    assert set(_connected_teleop().get_action()) == ALL_JOINTS


def test_the_connected_action_covers_every_joint_while_walking() -> None:
    """Held keys hand the legs to the engine; the neck joints still must appear."""
    t = _connected_teleop("w")
    t.get_action()  # first frame selects the motion
    assert set(t.get_action()) == ALL_JOINTS


def test_the_disconnected_action_covers_every_joint() -> None:
    """The pre-connect neutral action feeds the same frame builder."""
    assert set(_teleop().get_action()) == ALL_JOINTS


def test_the_action_matches_what_action_features_promised() -> None:
    t = _connected_teleop()
    assert set(t.get_action()) == set(t.action_features)
