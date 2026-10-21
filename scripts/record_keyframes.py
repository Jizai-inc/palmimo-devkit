#!/usr/bin/env python3
"""record_keyframes — pose the robot by hand and save the poses as keyframes.

See scripts/README.md for the options.

The robot must be lifted with every foot in the air: a limp leg carries no
weight, so a limp leg on the ground lets the body fall.
"""

import argparse
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from palmimo_sdk import DynamixelDriver, Keyframe, Palmimo, kinematics, palmimo_motor_ids, save_keyframes
from palmimo_sdk.keyframes import degrees_to_tick, joint_limits, tick_to_degrees
from palmimo_sdk.thermal import NECK_MOTORS


class _PoseSource(Protocol):
    def limp(self, joints: Sequence[str]) -> AbstractContextManager[None]: ...

    def read_pose(self, joints: Sequence[str] | None = None) -> dict[str, float]: ...


ALL_JOINTS: tuple[str, ...] = (*kinematics.LEG_MOTORS, *NECK_MOTORS)


def joints_for(legs: Sequence[int], neck: bool, joints: Sequence[str] = (), ids: Sequence[int] = ()) -> list[str]:
    """Servo names for *legs* (1-6), the neck axes when *neck* is set, *joints*, and servo *ids*.

    The selections combine; a joint picked twice is listed once, in first-picked order.
    """
    by_id = {motor_id: name for name, motor_id in palmimo_motor_ids().items()}
    names = list(kinematics.leg_motors(legs))
    if neck:
        names.extend(NECK_MOTORS)
    names.extend(joints)
    names.extend(by_id[motor_id] for motor_id in ids)
    return list(dict.fromkeys(names))


def arm_legs_of(joints: Sequence[str]) -> list[int]:
    """Legs with at least one released joint: off the ground, so judged as arms on playback."""
    return [leg for leg in kinematics.ALL_LEGS if any(name in joints for name in kinematics.leg_motors([leg]))]


def capture(
    robot: _PoseSource,
    joints: Sequence[str],
    poses: list[dict[str, float]],
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
    limits: Mapping[str, tuple[int, int]] | None = None,
) -> None:
    """Let *joints* go limp and append a pose to *poses* on every Enter until ``q``.

    ``u`` drops the last capture. *poses* is the caller's list, so whatever was
    captured survives an exception (a failed limp exit, Ctrl-C) for saving.
    A pose with a joint outside *limits* (joint -> tick range, as playback
    checks it) is not captured, so it is re-posed now rather than found
    unplayable at the end.
    """
    with robot.limp(joints):
        say("Limp. Pose by hand, then: Enter = capture, u = undo, q = finish.")
        while True:
            answer = ask(f"[{len(poses)} captured] > ").strip().lower()
            if answer == "q":
                break
            if answer == "u":
                if poses:
                    poses.pop()
                    say(f"Dropped capture {len(poses) + 1}.")
                else:
                    say("Nothing to undo.")
                continue
            if answer:
                say("Enter, u or q.")
                continue
            pose = robot.read_pose(joints)
            outside = [
                f"{name} {deg:+.1f} (playable {tick_to_degrees(lo):+.1f} to {tick_to_degrees(hi):+.1f})"
                for name, deg in pose.items()
                if limits and name in limits
                for lo, hi in [limits[name]]
                if not lo <= degrees_to_tick(deg) <= hi
            ]
            if outside:
                say("Not captured, outside the playback range: " + ", ".join(outside) + ". Re-pose and press Enter.")
                continue
            poses.append(pose)
            say(f"Capture {len(poses)}: " + ", ".join(f"{name} {deg:+.1f}" for name, deg in pose.items()))
    say("Holding the last pose; gains restored.")


def to_keyframes(poses: Sequence[dict[str, float]], step_s: float) -> list[Keyframe]:
    """Place each pose *step_s* seconds after the previous one, the first *step_s* after the start."""
    return [Keyframe(step_s * (i + 1), pose) for i, pose in enumerate(poses)]


def _legs(value: str) -> list[int]:
    try:
        legs = sorted({int(v) for v in value.split(",")})
    except ValueError:
        raise argparse.ArgumentTypeError(f"legs must be comma-separated numbers 1-6, got {value!r}") from None
    if not legs or any(leg not in kinematics.ALL_LEGS for leg in legs):
        raise argparse.ArgumentTypeError(f"legs must be between 1 and 6, got {value!r}")
    return legs


def _joint_names(value: str) -> list[str]:
    names = [v.strip() for v in value.split(",") if v.strip()]
    unknown = [name for name in names if name not in ALL_JOINTS]
    if not names or unknown:
        raise argparse.ArgumentTypeError(
            f"joints must be comma-separated servo names ({', '.join(ALL_JOINTS)}), got {value!r}"
        )
    return names


def _ids(value: str) -> list[int]:
    valid = sorted(palmimo_motor_ids().values())
    try:
        ids = [int(v) for v in value.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError(f"ids must be comma-separated numbers, got {value!r}") from None
    if any(motor_id not in valid for motor_id in ids):
        raise argparse.ArgumentTypeError(f"ids must be between {valid[0]} and {valid[-1]}, got {value!r}")
    return ids


def _step(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"step must be a number of seconds, got {value!r}") from None
    if not (math.isfinite(seconds) and seconds > 0):
        raise argparse.ArgumentTypeError(f"step must be a positive number of seconds, got {value!r}")
    return seconds


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[1],
        epilog="Lift the robot with every foot in the air first: released joints carry no weight.",
    )
    parser.add_argument("--legs", type=_legs, action="extend", default=[], help="legs to release, e.g. 6 or 3,6")
    parser.add_argument("--neck", action="store_true", help="release the neck too")
    parser.add_argument(
        "--joints",
        type=_joint_names,
        action="extend",
        default=[],
        help="single joints to release, e.g. leg_6_pitch1,neck_yaw",
    )
    parser.add_argument(
        "--ids", type=_ids, action="extend", default=[], help="single servos to release by ID (1-21), e.g. 17,18"
    )
    parser.add_argument("--step", type=_step, default=1.5, help="seconds between keyframes (default 1.5)")
    parser.add_argument("--out", type=Path, default=Path("keyframes.json"), help="output file")
    parser.add_argument("--port", "-p", default=None, help="servo bus port (auto-detected when omitted)")
    args = parser.parse_args(argv)
    joints = joints_for(args.legs, args.neck, args.joints, args.ids)
    if not joints:
        parser.error("choose at least one of --legs / --neck / --joints / --ids")
    if not args.out.resolve().parent.is_dir():
        parser.error(f"the folder for --out does not exist: {args.out.resolve().parent}")

    print("Lift the robot so every foot is in the air: released joints carry no weight.")
    print(f"Releasing: {', '.join(joints)}")
    if input("Type 'yes' when the robot is lifted: ").strip().lower() != "yes":
        print("Not started.")
        return 1

    poses: list[dict[str, float]] = []
    with Palmimo(driver=DynamixelDriver(port=args.port)) as robot:
        try:
            center = robot.engine.neck_pitch_center()
            limits = {name: joint_limits(name, center) for name in joints}
            capture(robot, joints, poses, ask=input, say=print, limits=limits)
        finally:
            keyframes = to_keyframes(poses, args.step)
            if keyframes:
                save_keyframes(args.out, keyframes)
                print(f"Saved {len(keyframes)} keyframes to {args.out}")
            else:
                print("No keyframes captured; nothing saved.")
        if keyframes and input("Play them back now? [y/N] ").strip().lower() == "y":
            try:
                robot.play_keyframes(keyframes, arm_legs=arm_legs_of(joints))
            except ValueError as exc:
                print(f"Not played: {exc}")
                print("Edit the times or angles in the file (a longer --step gives every move more time).")
        # The exit park eases home by a fixed fraction per frame, which is fast
        # from a hand-posed leg; glide on a timed curve first.
        robot.return_to_neutral(duration=1.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
