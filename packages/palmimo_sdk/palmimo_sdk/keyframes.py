# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Keyframe tracks — timed joint poses played back with raised-cosine easing.

Pure computation, like :mod:`palmimo_sdk.engine`: a :class:`KeyframeTrack`
turns a list of :class:`Keyframe` poses into the tick value of every joint at
any time, and validates range and speed before anything moves. Playing a track
on the robot is :meth:`palmimo_sdk.robot.Palmimo.play_keyframes`'s job.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .engine import MotionEngine
from .io.base import SAFE_MAX_TICK, SAFE_MIN_TICK
from .kinematics import DEG_PER_TICK, LEG_MOTORS, NEUTRAL, TICK_PER_DEG
from .thermal import NECK_MOTORS


JOINTS: tuple[str, ...] = LEG_MOTORS + NECK_MOTORS

# Peak joint speed a track may command, in degrees per second. Below the
# XC330-M288's no-load speed (about 486 deg/s), so the servo can follow the
# commanded curve, and above the shipped head shake (about 210 deg/s).
MAX_JOINT_SPEED_DEG_S = 360.0

# A raised-cosine segment of span d over T seconds peaks at (pi/2)*d/T.
_RAISED_COSINE_PEAK = math.pi / 2.0


@dataclass(frozen=True)
class Keyframe:
    """One timed pose in a keyframe track.

    Attributes:
        time (float): Seconds from the start of playback at which the pose is
            reached. Must be positive; keyframes must be in increasing order.
        joints (Mapping[str, float]): Joint name -> angle in degrees from the
            servo neutral (tick 2048). Names are the servo names
            (``leg_{1-6}_{yaw,pitch1,pitch2}``, ``neck_yaw``, ``neck_pitch1``,
            ``neck_pitch2``). A joint left out keeps the angle it had at the
            previous keyframe (or at the start of playback).
    """

    time: float
    joints: Mapping[str, float] = field(default_factory=dict)


def degrees_to_tick(degrees: float) -> int:
    """Convert an angle from the servo neutral to a raw tick."""
    return NEUTRAL + round(degrees * TICK_PER_DEG)


def tick_to_degrees(tick: int) -> float:
    """Convert a raw tick to an angle from the servo neutral."""
    return (tick - NEUTRAL) * DEG_PER_TICK


def joint_limits(joint: str, neck_pitch_center: int = NEUTRAL) -> tuple[int, int]:
    """Tick range a keyframe may command for *joint*.

    The neck axes keep to the range the engine's own neck motions reach: the
    servo-neutral band, and for ``neck_pitch1`` also the same amplitude around
    its rest-trimmed front (*neck_pitch_center*). The legs keep to the driver's
    safe range.
    """
    if joint in NECK_MOTORS:
        amp = MotionEngine.NECK_AMPLITUDE_TICKS
        low, high = NEUTRAL - amp, NEUTRAL + amp
        if joint == "neck_pitch1":
            low, high = max(low, neck_pitch_center - amp), min(high, neck_pitch_center + amp)
        return low, high
    return SAFE_MIN_TICK, SAFE_MAX_TICK


class KeyframeTrack:
    """Per-joint timelines built from a start pose and a list of keyframes.

    Every keyframe is a full pose: the joints it names move to the given
    angles and every other joint holds its previous value. Between two poses
    each joint eases in and out with a raised cosine, so the robot comes to
    rest at every keyframe. Construction validates the whole track and raises
    before anything moves.

    Args:
        start (Mapping[str, int]): The pose playback starts from (joint -> tick),
            normally the robot's current pose. Must cover every joint.
        keyframes (Sequence[Keyframe]): The poses to reach, in time order.
        neck_pitch_center (int): ``neck_pitch1``'s rest-trimmed front tick
            (:meth:`MotionEngine.neck_pitch_center`), which narrows its range.

    Raises:
        ValueError: The track is empty, a time is not positive or not
            increasing, a joint name is unknown, an angle is outside the joint's
            range, or a segment would exceed ``MAX_JOINT_SPEED_DEG_S``.
    """

    def __init__(
        self, start: Mapping[str, int], keyframes: Sequence[Keyframe], *, neck_pitch_center: int = NEUTRAL
    ) -> None:
        if not keyframes:
            raise ValueError("A keyframe track needs at least one keyframe.")
        missing = [j for j in JOINTS if j not in start]
        if missing:
            raise ValueError(f"Start pose is missing joints: {missing}")
        pose = {j: int(start[j]) for j in JOINTS}
        timelines: dict[str, list[tuple[float, float]]] = {j: [(0.0, float(t))] for j, t in pose.items()}
        prev_time = 0.0
        for index, key in enumerate(keyframes):
            if not math.isfinite(key.time) or key.time <= prev_time:
                raise ValueError(
                    f"Keyframe {index}: time {key.time} must be finite and later than {prev_time} "
                    "(times start after 0 and increase)."
                )
            for joint, degrees in key.joints.items():
                if joint not in pose:
                    raise ValueError(f"Keyframe {index}: unknown joint {joint!r}. Choose from: {', '.join(JOINTS)}")
                if not math.isfinite(degrees):
                    raise ValueError(f"Keyframe {index}: {joint} angle {degrees} is not a finite number.")
                tick = degrees_to_tick(degrees)
                low, high = joint_limits(joint, neck_pitch_center)
                if not low <= tick <= high:
                    raise ValueError(
                        f"Keyframe {index}: {joint} at {degrees:.1f} deg is outside its range "
                        f"[{tick_to_degrees(low):.1f}, {tick_to_degrees(high):.1f}] deg."
                    )
                pose[joint] = tick
            span_s = key.time - prev_time
            for joint, tick in pose.items():
                last = timelines[joint][-1][1]
                peak = _RAISED_COSINE_PEAK * abs(tick - last) * DEG_PER_TICK / span_s
                if peak > MAX_JOINT_SPEED_DEG_S:
                    raise ValueError(
                        f"Keyframe {index}: {joint} would move {abs(tick - last) * DEG_PER_TICK:.1f} deg in "
                        f"{span_s:.2f} s (peak {peak:.0f} deg/s, limit {MAX_JOINT_SPEED_DEG_S:.0f} deg/s). "
                        f"Give it at least {self.min_seconds(abs(tick - last)):.2f} s."
                    )
                timelines[joint].append((key.time, float(tick)))
            prev_time = key.time
        self._timelines = timelines
        self._duration = prev_time

    @staticmethod
    def min_seconds(span_ticks: float) -> float:
        """Shortest segment that moves *span_ticks* without exceeding the speed limit."""
        return _RAISED_COSINE_PEAK * span_ticks * DEG_PER_TICK / MAX_JOINT_SPEED_DEG_S

    @property
    def duration(self) -> float:
        """Seconds from the start of playback to the last keyframe."""
        return self._duration

    @property
    def end_pose(self) -> dict[str, int]:
        """The pose held after the last keyframe (joint -> tick)."""
        return {j: int(keys[-1][1]) for j, keys in self._timelines.items()}

    def sample(self, t: float) -> dict[str, int]:
        """Pose at *t* seconds into playback (joint -> tick); holds the end pose past :attr:`duration`."""
        return {j: round(MotionEngine._interp_keyframes(keys, t)) for j, keys in self._timelines.items()}
