"""Tests for the keyframe recording CLI."""

from __future__ import annotations

import argparse
import builtins
import contextlib
import importlib.util
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from palmimo_sdk import Keyframe, MotionEngine


_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "record_keyframes.py"
_SPEC = importlib.util.spec_from_file_location("record_keyframes", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
record_keyframes = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(record_keyframes)


class _FakeRobot:
    """Stands in for Palmimo: hands out a scripted pose per read and tracks the limp block."""

    def __init__(self, poses: list[dict[str, float]]) -> None:
        self._poses = iter(poses)
        self.limp_joints: list[str] | None = None
        self.limp_open = False
        self.reads_while_limp: list[bool] = []

    @contextlib.contextmanager
    def limp(self, joints: Sequence[str]) -> Iterator[None]:
        self.limp_joints = list(joints)
        self.limp_open = True
        try:
            yield
        finally:
            self.limp_open = False

    def read_pose(self, joints: Sequence[str] | None = None) -> dict[str, float]:
        self.reads_while_limp.append(self.limp_open)
        return next(self._poses)


def _answers(*replies: str) -> Iterator[str]:
    return iter(replies)


def test_capture_turns_each_enter_into_a_keyframe_one_step_apart() -> None:
    robot = _FakeRobot([{"leg_6_pitch1": -60.0}, {"leg_6_pitch1": 35.0}])
    replies = _answers("", "", "q")
    poses: list[dict[str, float]] = []

    record_keyframes.capture(robot, ["leg_6_pitch1"], poses, ask=lambda _: next(replies), say=lambda _: None)

    assert record_keyframes.to_keyframes(poses, 1.5) == [
        Keyframe(1.5, {"leg_6_pitch1": -60.0}),
        Keyframe(3.0, {"leg_6_pitch1": 35.0}),
    ]
    assert robot.limp_joints == ["leg_6_pitch1"]
    assert robot.reads_while_limp == [True, True]
    assert not robot.limp_open


def test_capture_undo_drops_the_last_pose_and_keeps_the_timing_even() -> None:
    robot = _FakeRobot([{"leg_6_yaw": 1.0}, {"leg_6_yaw": 2.0}, {"leg_6_yaw": 3.0}])
    replies = _answers("", "", "u", "", "q")
    poses: list[dict[str, float]] = []

    record_keyframes.capture(robot, ["leg_6_yaw"], poses, ask=lambda _: next(replies), say=lambda _: None)

    assert record_keyframes.to_keyframes(poses, 2.0) == [
        Keyframe(2.0, {"leg_6_yaw": 1.0}),
        Keyframe(4.0, {"leg_6_yaw": 3.0}),
    ]


def test_capture_ignores_unknown_input_and_undo_on_nothing() -> None:
    robot = _FakeRobot([])
    replies = _answers("u", "x", "q")
    said: list[str] = []

    poses: list[dict[str, float]] = []
    record_keyframes.capture(robot, ["leg_6_yaw"], poses, ask=lambda _: next(replies), say=said.append)
    assert poses == []
    assert "Nothing to undo." in said
    assert "Enter, u or q." in said


def test_joints_for_expands_legs_and_neck() -> None:
    assert record_keyframes.joints_for([6], neck=False) == ["leg_6_yaw", "leg_6_pitch1", "leg_6_pitch2"]
    assert record_keyframes.joints_for([], neck=True) == ["neck_pitch1", "neck_pitch2", "neck_yaw"]


def test_joints_for_combines_legs_neck_joint_names_and_servo_ids_without_repeats() -> None:
    joints = record_keyframes.joints_for([6], neck=False, joints=["neck_yaw", "leg_6_yaw"], ids=[1, 21])

    assert joints == ["leg_6_yaw", "leg_6_pitch1", "leg_6_pitch2", "neck_yaw", "leg_1_yaw"]


def test_joints_for_maps_servo_ids_to_joint_names() -> None:
    assert record_keyframes.joints_for([], neck=False, ids=[18, 19]) == ["leg_6_pitch2", "neck_pitch1"]


def test_arm_legs_of_lists_every_leg_with_a_released_joint() -> None:
    assert record_keyframes.arm_legs_of(["neck_yaw", "leg_3_pitch2", "leg_6_yaw", "leg_6_pitch1"]) == [3, 6]
    assert record_keyframes.arm_legs_of(["neck_yaw"]) == []


@pytest.mark.parametrize("value", ["", "leg_7_yaw", "leg_6_pitch1,tail"])
def test_joints_argument_rejects_unknown_names(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        record_keyframes._joint_names(value)


@pytest.mark.parametrize("value", ["0", "22", "x", ""])
def test_ids_argument_rejects_anything_but_palmimo_servo_ids(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        record_keyframes._ids(value)


def test_main_releases_single_servos_by_id_and_judges_their_leg_as_an_arm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _FacadeStub([{"leg_6_pitch1": -20.0}], fail_on_exit=False)

    _, code = _run_main(monkeypatch, tmp_path, stub, _answers("yes", "", "q", "y"), select=("--ids", "17"))

    assert code == 0
    assert stub.limp_joints == ["leg_6_pitch1"]
    assert [arm_legs for _, arm_legs in stub.played] == [[6]]


@pytest.mark.parametrize("value", ["0", "-1", "inf", "nan", "x"])
def test_step_argument_rejects_anything_but_a_positive_finite_number(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        record_keyframes._step(value)


@pytest.mark.parametrize("value", ["0", "7", "a", ""])
def test_legs_argument_rejects_anything_but_leg_ids(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        record_keyframes._legs(value)


def test_main_does_not_connect_until_the_robot_is_confirmed_lifted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builtins, "input", lambda _: "no")

    def fail(*_: object, **__: object) -> None:
        raise AssertionError("connected without confirmation")

    monkeypatch.setattr(record_keyframes, "DynamixelDriver", fail)

    assert record_keyframes.main(["--legs", "6"]) == 1


def test_main_requires_something_to_release() -> None:
    with pytest.raises(SystemExit):
        record_keyframes.main([])


class _FacadeStub(_FakeRobot):
    """Stands in for the connected Palmimo inside main(); limp's exit can be made to fail."""

    def __init__(self, poses: list[dict[str, float]], fail_on_exit: bool) -> None:
        super().__init__(poses)
        self.fail_on_exit = fail_on_exit
        self.played: list[tuple[list[Keyframe], object]] = []
        self.engine = MotionEngine()

    @contextlib.contextmanager
    def limp(self, joints: Sequence[str]) -> Iterator[None]:
        self.limp_joints = list(joints)
        yield
        if self.fail_on_exit:
            raise RuntimeError("No position read from ['leg_6_yaw']; left limp at zero gain.")

    def play_keyframes(self, keyframes: list[Keyframe], arm_legs: object = ()) -> None:
        self.played.append((keyframes, arm_legs))

    def return_to_neutral(self, duration: float | None = None) -> None:
        pass

    def __enter__(self) -> _FacadeStub:
        return self

    def __exit__(self, *_: object) -> None:
        pass


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stub: _FacadeStub,
    replies: Iterator[str],
    select: Sequence[str] = ("--legs", "6"),
) -> tuple[Path, int | None]:
    monkeypatch.setattr(builtins, "input", lambda _: next(replies))
    monkeypatch.setattr(record_keyframes, "DynamixelDriver", lambda **_: object())
    monkeypatch.setattr(record_keyframes, "Palmimo", lambda **_: stub)
    out = tmp_path / "track.json"
    try:
        code: int | None = record_keyframes.main([*select, "--out", str(out)])
    except RuntimeError:
        code = None
    return out, code


def test_main_saves_what_was_captured_even_when_the_limp_exit_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _FacadeStub([{"leg_6_yaw": 4.0}], fail_on_exit=True)

    out, code = _run_main(monkeypatch, tmp_path, stub, _answers("yes", "", "q"))

    assert code is None  # the failure still surfaces
    assert out.is_file()
    assert "leg_6_yaw" in out.read_text()


def test_main_preview_judges_the_recorded_legs_as_arms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stub = _FacadeStub([{"leg_6_yaw": 4.0}], fail_on_exit=False)

    _, code = _run_main(monkeypatch, tmp_path, stub, _answers("yes", "", "q", "y"))

    assert code == 0
    assert [arm_legs for _, arm_legs in stub.played] == [[6]]


def test_main_rejects_an_output_folder_that_does_not_exist_before_connecting(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        record_keyframes.main(["--legs", "6", "--out", str(tmp_path / "missing" / "track.json")])


def test_main_adds_up_a_selection_flag_given_twice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stub = _FacadeStub([], fail_on_exit=False)

    _run_main(monkeypatch, tmp_path, stub, _answers("yes", "q"), select=("--ids", "17", "--ids", "21"))

    assert stub.limp_joints == ["leg_6_pitch1", "neck_yaw"]


def test_capture_refuses_a_pose_outside_the_playback_range_and_keeps_going() -> None:
    from palmimo_sdk.keyframes import degrees_to_tick

    robot = _FakeRobot([{"neck_pitch1": 23.9}, {"neck_pitch1": 5.0}])
    replies = _answers("", "", "q")
    said: list[str] = []
    poses: list[dict[str, float]] = []

    record_keyframes.capture(
        robot,
        ["neck_pitch1"],
        poses,
        ask=lambda _: next(replies),
        say=said.append,
        limits={"neck_pitch1": (degrees_to_tick(-26.4), degrees_to_tick(11.3))},
    )

    assert poses == [{"neck_pitch1": 5.0}]
    assert any("Not captured" in line and "neck_pitch1 +23.9" in line for line in said)
