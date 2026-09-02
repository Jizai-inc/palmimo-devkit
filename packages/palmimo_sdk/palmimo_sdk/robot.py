# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Palmimo — the SDK facade for the Palmimo hexapod.

This is the single entry point users import. It owns state and the
connection lifecycle, delegates motion computation to
:class:`~palmimo_sdk.engine.MotionEngine`, and delegates servo I/O to a
:class:`~palmimo_sdk.io.base.ServoDriver`.

Without a driver the facade is compute-only — :meth:`step` returns the
computed servo positions, handy for simulation, tests, or piping elsewhere::

    from palmimo_sdk import Palmimo

    robot = Palmimo()
    robot.forward()
    for _ in range(100):
        pos = robot.step()   # dict like {"leg_1_yaw": 2048, ...}

For wall-clock control, set a motion then :meth:`run` it for a duration or a
step count — paced at the facade's control rate (:attr:`fps`), so you never
count raw control cycles::

    robot = Palmimo()
    robot.forward()
    robot.run(seconds=1.5)       # blocking, real-time paced
    robot.rotate_left()
    robot.run(steps=90)          # 90 control cycles ≈ 90 / fps seconds
    robot.stop()

To drive hardware, attach a concrete :class:`~palmimo_sdk.io.base.ServoDriver`
and use a ``with`` block so connect (which wakes the robot) / park (neutral
return + neck soft-release) / disconnect are handled for you.
:class:`~palmimo_sdk.io.DynamixelDriver` ships for the Dynamixel bus; inject
your own (e.g. an in-memory fake) to run without hardware::

    with Palmimo(driver=my_driver) as robot:
        robot.forward()
        robot.run(seconds=1.5)
        robot.stop()

Choreography — a routine is a sequence of :class:`RoutineStep` cues (each a
leg :class:`~palmimo_sdk.engine.Motion` plus an optional neck sweep)::

    routine = [
        RoutineStep(Motion.FORWARD, seconds=2.0),
        RoutineStep(Motion.ROTATE_LEFT, seconds=1.0),
        RoutineStep(Motion.DANCE, seconds=3.0),
        RoutineStep(Motion.FORWARD, seconds=2.0, neck_sweep=True),  # walk + look around
        RoutineStep(Motion.IDLE, seconds=0.5),
    ]
    for pos in robot.play(routine):
        ...

Legacy ``(name, seconds)`` tuples (with ``"look_around"`` as a special name)
are still accepted and coerced to :class:`RoutineStep`.
"""

from __future__ import annotations

import contextlib
import math
import signal
import threading
import time
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from ._signals import signals_ignored
from .engine import NECK_GESTURES, Motion, MotionEngine
from .io import FaceDisplay, HeadCamera, Microphone, MicStream, ServoDriver, Speaker, SpeechHandle


if TYPE_CHECKING:
    from types import TracebackType


# Neck soft-release: the neck holds the head against gravity, so cutting torque
# while it actively holds snaps the head. Instead, step Position_P_Gain down on
# the neck so the head eases to a near-limp hold, then torque-off releases almost
# no stored force (measured release jolt ~0 ticks). No fixed rest pose to maintain
# — the head settles wherever the fading hold + joint friction leave it.
#
# The head comes to rest on the torso top plate, so the descent must be gentle
# enough that it touches down softly (no audible tap). Droop under a fixed
# gravity torque scales ~1/gain, so the schedule steps down in roughly even
# *ratios* (each step adds a similar bit of droop) and runs the tail to a near-
# zero gain — the head is already seated by torque-off, so cutting torque is a
# non-event rather than a final drop onto the plate.
_NECK_MOTORS = ("neck_pitch1", "neck_pitch2", "neck_yaw")
_NECK_RELEASE_GAINS = (700, 550, 430, 330, 250, 190, 140, 100, 70, 48, 32, 20, 12, 6)
_NECK_RELEASE_STEP_S = 0.18

# Startup wake-up: glide to neutral while ramping Position_P_Gain from a soft
# start up to the default, so the robot firms up and rises (the inverse of the
# neck soft-release) instead of snapping to a stiff neutral.
_WAKE_START_GAIN = 300
_WAKE_FULL_GAIN = 900

# Official wave per-axis servo tuning. Applied
# automatically while a wave is active and restored when it ends, so any caller
# (voice, greeting, demo) gets the tuned feel without passing knobs. The waving
# arm runs PV=0 (crisp stroke tracking) + a command-side IIR low-pass to soften
# it; the body-tilt legs stay at the driver default PV (300) for buttery lean.
_WAVE_ARM_PV = 0  # Profile_Velocity on the waving arm (0 = no profile, crisp)
_WAVE_LEG_PV = 300  # Profile_Velocity elsewhere (= driver default; smooth body tilt)
_WAVE_GAIN = 1300  # Position_P_Gain (all axes) — tightens the quick strokes
_WAVE_ARM_IIR = 0.1  # IIR alpha on the waving arm (smaller = heavier smoothing)
# The clap's arms get a much lighter IIR: alpha 0.1 has a ~170 ms time constant,
# which attenuated the original 2.4 Hz open/close to ~36% — measured on hardware
# as the hands stopping ~7° short of the commanded close, i.e. the clap never
# visually closed. 0.5 passes ~94% at 2.4 Hz. At the shipped default tempo of
# 5 Hz (clap_period 0.2), 0.5 passes ~81% — measured closest approach ~31 mm
# against clap_gap=20, a rounded-off close that errs away from contact; slow
# clap_period to ≤0.4 s when the gap needs to be hit exactly. (The wave keeps
# 0.1.)
_CLAP_ARM_IIR = 0.5

# Stretch per-servo tuning: the quickest element of the final planted-feet
# stretch is the wind-up spring (the ~0.2 s dip-to-rise turnaround); the
# default time-based Profile_Velocity (300 ms per move) was measured leaving
# the legs ~40% short on fast strokes, so a 120 ms profile keeps the spring
# readable. Restored to the default on exit.
_STRETCH_LEG_PV = 120
# Neck-gesture servo tuning (nod / head-shake). The gestures stroke in ~200 ms,
# but the driver-default time-based Profile_Velocity (300 ≈ 300 ms per goal)
# would smear those strokes into mush — so while a gesture is active the two
# driven neck axes run PV=0 (track each streamed goal crisply; the command
# stream itself is already raised-cosine smooth) and the driver's OWN default
# profile is restored when it ends (fallback below for drivers that don't
# expose it). Only the axes the engine drives are touched.
_NECK_GESTURE_MOTORS = ("neck_pitch1", "neck_yaw")
_NECK_GESTURE_PV = 0
_NECK_GESTURE_RESTORE_PV_FALLBACK = 300
# Dance finishing flourish — a lingering hold followed by a slow return,
# tuned on hardware. After the swaying body ends
# near-static on the dwell at an extreme, the legs softly hold that ACTUAL pose
# (a lowered Position_P_Gain, so they neither creep out to the full-amplitude
# goal they were lagging toward — which overshoots — nor clunk to a hard stop),
# then the whole body glides home. The neck is deliberately left out of the hold
# so it stays commanded to neutral (head up) at full gain.
_DANCE_SWAYS = 3  # half-cycles to sway before finishing
_DANCE_END_HOLD_S = 0.2  # lingering hold at the final extreme
_DANCE_SETTLE_S = 1.0  # glide-home duration (larger = slower/softer)
_DANCE_HOLD_GAIN = 300  # leg Position_P_Gain during the soft end-hold
# Poll granularity for perform_dance's non-paced hold/glide sleeps (see
# Palmimo._sleep_cancellable) -- small enough that a cancel() lands promptly,
# large enough not to busy-loop.
_CANCEL_POLL_INTERVAL_S = 0.05

# IDLE streamed after a MotionCancelled unwinds perform_dance, gliding the
# legs onto a stance pose instead of leaving them frozen mid-stride (bent and
# loaded) while the caller decides its next move. Matches the agent tool
# layer's post-motion stance settle duration.
_CANCEL_SETTLE_S = 0.5


@contextlib.contextmanager
def _deferred_interrupts() -> Iterator[None]:
    """Suppress SIGINT for the duration of the block, so it runs to the end.

    For a shutdown ramp that must not be cut short: no
    :class:`KeyboardInterrupt` is raised inside the block, at any point. Also
    guards :meth:`Palmimo.sleep`'s neck soft-release, which is not a shutdown
    — the process keeps running — but the same reasoning applies: a press
    landing mid-ramp there is likewise dropped rather than re-raised.

    A SIGINT that arrives inside is dropped rather than re-raised on exit — the
    block *is* the shutdown (or soft-release) the Ctrl+C asked to interrupt.
    Presses after it behave normally.

    A no-op off the main thread, where Python never delivers SIGINT, and on an
    interpreter that refuses handler installation; the block then runs as it
    would without this guard.

    A thin wrapper over :func:`~palmimo_sdk._signals.signals_ignored`, which
    also backs the MCP server's own shutdown guard.
    """
    with signals_ignored(signal.SIGINT):
        yield


@dataclass(frozen=True)
class NeckPitchDegrees:
    """A neck PITCH angle expressed in real degrees.

    Self-validating: construction raises :class:`ValueError` if *value* falls
    outside the neck's actual mechanical pitch travel
    (:attr:`~palmimo_sdk.engine.MotionEngine.NECK_PITCH_TRAVEL_DEG`, ~26.4 deg)
    — range-checking is this value object's own responsibility, not
    :meth:`Palmimo.look`'s, so an out-of-range request fails loudly at the
    call site instead of silently saturating. Converted by :meth:`Palmimo.look`
    into the engine's normalized [-1, 1] input by dividing by that same travel.

    A separate class from :class:`NeckYawDegrees` on purpose: the two axes'
    travel limits happen to be equal today (both derive from the shared
    ``NECK_AMPLITUDE_TICKS``), but nothing here assumes that stays true, and
    keeping them distinct types also lets :meth:`Palmimo.look` reject an
    axis-swapped argument (e.g. a yaw value passed as ``pitch=``) with a clear
    :class:`TypeError` instead of silently accepting it.
    """

    value: float

    def __post_init__(self) -> None:
        travel = MotionEngine.NECK_PITCH_TRAVEL_DEG
        if abs(self.value) > travel:
            raise ValueError(
                f"NeckPitchDegrees({self.value!r}) exceeds the neck's actual pitch travel of ±{travel:.2f} deg."
            )


@dataclass(frozen=True)
class NeckYawDegrees:
    """A neck YAW angle expressed in real degrees.

    Mirrors :class:`NeckPitchDegrees` for the yaw axis: self-validates at
    construction against
    :attr:`~palmimo_sdk.engine.MotionEngine.NECK_YAW_TRAVEL_DEG` and raises
    :class:`ValueError` past it, and is converted by :meth:`Palmimo.look` by
    dividing by that same travel.
    """

    value: float

    def __post_init__(self) -> None:
        travel = MotionEngine.NECK_YAW_TRAVEL_DEG
        if abs(self.value) > travel:
            raise ValueError(
                f"NeckYawDegrees({self.value!r}) exceeds the neck's actual yaw travel of ±{travel:.2f} deg."
            )


@dataclass(frozen=True)
class NeckPitchNormalized:
    """A neck PITCH angle as a fraction [-1, 1] of the mechanical travel.

    Self-validating: construction raises :class:`ValueError` outside [-1, 1].
    Passed straight through to :meth:`Palmimo.look` / the engine's
    ``set_neck`` unchanged.
    """

    value: float

    def __post_init__(self) -> None:
        if not -1.0 <= self.value <= 1.0:
            raise ValueError(f"NeckPitchNormalized({self.value!r}) must be within [-1, 1].")


@dataclass(frozen=True)
class NeckYawNormalized:
    """A neck YAW angle as a fraction [-1, 1] of the mechanical travel.

    Mirrors :class:`NeckPitchNormalized` for the yaw axis.
    """

    value: float

    def __post_init__(self) -> None:
        if not -1.0 <= self.value <= 1.0:
            raise ValueError(f"NeckYawNormalized({self.value!r}) must be within [-1, 1].")


@dataclass(frozen=True)
class RoutineStep:
    """One cue in a choreography routine.

    Args:
        motion (Motion): Leg motion to run for this cue. Default :attr:`Motion.IDLE`.
        seconds (float): Wall-clock duration of the cue.
        neck_sweep (bool): Overlay a sinusoidal neck sweep — the old ``"look_around"`` — on top
            of the leg *motion*. The neck pans/tilts while the legs keep running
            *motion*, then recenters when the cue ends.
        wave_leg (int, optional): Leg ID to wave (3 = front-left, 6 = front-right) when *motion* is
            :attr:`Motion.WAVE`. ``None`` keeps whatever leg was last selected.
            Set automatically for the legacy ``"wave_left"`` / ``"wave_right"`` cues
            so the string and choreography APIs pick the same side.
    """

    motion: Motion = Motion.IDLE
    seconds: float = 0.0
    neck_sweep: bool = False
    wave_leg: int | None = None


class MotionCancelled(Exception):  # noqa: N818  (a cooperative interrupt signal, not an error -- mirrors asyncio.CancelledError's own naming)
    """Raised inside :meth:`Palmimo.run` (and any other :meth:`Palmimo._pace`-driven
    call — :meth:`Palmimo.perform_dance`, :meth:`Palmimo.play_realtime`) when
    :meth:`Palmimo.cancel` was called while that motion was in flight.

    This is how a caller running the blocking, real-time-paced ``run()`` on
    one thread gets interrupted from another — e.g. an agent racing a
    long-running tool call against a barge-in. The exception is checked and
    raised once per frame, inside ``_pace``'s loop, so it unwinds ``run()``
    within roughly one frame period rather than waiting for the whole timed
    call to finish. A caller that wants the robot to land back in a stance
    after being cancelled catches this around ``run()`` and calls
    :meth:`Palmimo.stop` in a ``finally`` — exactly the pattern
    :func:`~palmimo_sdk.agent.tools._run_motion` already uses for any other
    mid-motion failure, so ``MotionCancelled`` needs no special handling
    there; it is caught by the same ``except Exception`` a tool-calling loop
    already has.

    Never raised anywhere else in the SDK.
    """


class Palmimo:
    """SDK facade for the Palmimo hexapod.

    Owns motion state and the connection lifecycle. Motion computation is
    delegated to :class:`~palmimo_sdk.engine.MotionEngine` and servo I/O to a
    :class:`~palmimo_sdk.io.base.ServoDriver`.

    Args:
        driver (ServoDriver, optional): Servo backend. When attached, :meth:`step` streams positions to it
            and the ``with`` protocol manages the full lifecycle: :meth:`connect`
            auto-wakes (limp -> gain-ramped rise to neutral, unless
            ``auto_wake=False``) and exit parks (neutral return + neck
            soft-release) before disconnect. When ``None``, the facade is
            compute-only (sim/tests).
        auto_wake (bool): When a driver is attached, whether :meth:`connect` automatically runs the
            :meth:`wake` glide (limp -> gain-ramped rise to neutral) after all
            resources open successfully. ``True`` by default. Pass ``False``
            for an immediate connect with no glide — sim, calibration, or
            tests that don't want the extra motion.
        display (FaceDisplay, optional): Face display resource. When attached, the connection lifecycle bundles
            it alongside the driver — :meth:`connect` opens it and plays the
            boot-wake animation, :meth:`disconnect` returns it to IDLE and closes
            it — and :meth:`set_expression` drives it. Works even compute-only
            (``driver=None``): a face with no legs. ``None`` (default) makes the
            expression API a no-op.
        speaker (Speaker, optional): Text-to-speech resource (piper-plus). When attached, the connection
            lifecycle bundles it — :meth:`connect` opens it (a piper availability
            probe) and :meth:`disconnect` closes it — and :meth:`say` speaks through
            it. Works compute-only like *display*. ``None`` (default) makes
            :meth:`say` a no-op.
        camera (HeadCamera, optional): Head camera resource. When attached, the connection lifecycle bundles
            it alongside driver/display/speaker — :meth:`connect` opens it and
            starts its background frame drain, :meth:`disconnect` closes it (which
            stops the drain first — see :meth:`~palmimo_sdk.io.camera.HeadCamera.close`).
            The facade does not read frames itself (no ``capture()`` method here);
            consumers reach the camera through the :attr:`camera` property, the
            same way :attr:`driver` exposes the servo bus for direct writes. Works
            compute-only like *display* / *speaker*. ``None`` (default) means no
            camera is wired.
        mic (Microphone or MicStream, optional): USB microphone resource — either a one-shot
            :class:`~palmimo_sdk.io.microphone.Microphone` (shells out per
            recording) or a shared :class:`~palmimo_sdk.io.mic_stream.MicStream`
            (owns one background-read capture, fanned out to subscribers). Both
            expose the same ``open()`` / ``close()`` / ``is_open`` contract the
            facade needs, so either works here without any branching in
            :meth:`connect` / :meth:`disconnect`. Bundled by the connection
            lifecycle like the other peripherals — :meth:`connect` opens it,
            :meth:`disconnect` closes it. Like *camera*, the facade does not
            record audio itself; consumers reach it through the :attr:`mic`
            property and call :meth:`~palmimo_sdk.io.microphone.Microphone.record`
            / :meth:`~palmimo_sdk.io.mic_stream.MicStream.record` (or
            :meth:`~palmimo_sdk.io.mic_stream.MicStream.subscribe`) on it. An app
            that runs its own capture stack (streaming VAD, say) simply leaves
            this ``None`` and owns that stack itself. ``None`` (default) means no
            mic is wired.
        gait_speed (float): Gait phase increment per :meth:`step` call.
        step_length (float): Walking stride length in mm.
        step_height (float): Swing-leg lift height in mm for the walk and rotate gaits.
        on_step (callable, optional): Callback invoked with the position dict after every :meth:`step`,
            in addition to (and after) any driver write.
        fps (int): Control rate in cycles per second. The single source of truth for
            translating between seconds and steps in :meth:`run` and :meth:`play`.
    """

    _MOTION_MAP: ClassVar[dict[str, Motion]] = {
        "idle": Motion.IDLE,
        "stop": Motion.IDLE,
        "forward": Motion.FORWARD,
        "backward": Motion.BACKWARD,
        "strafe_left": Motion.STRAFE_LEFT,
        "strafe_right": Motion.STRAFE_RIGHT,
        "rotate_left": Motion.ROTATE_LEFT,
        "rotate_right": Motion.ROTATE_RIGHT,
        "dance": Motion.DANCE,
        "body_tilt": Motion.BODY_TILT,
        "tilt": Motion.BODY_TILT,
        "pushup": Motion.PUSHUP,
        "push_up": Motion.PUSHUP,
        "wave": Motion.WAVE,
        "wave_right": Motion.WAVE,
        "wave_left": Motion.WAVE,
        "wave_both": Motion.WAVE_BOTH,
        "clap": Motion.CLAP,
        "creep": Motion.CREEP,
        "bow": Motion.BOW,
        "stretch": Motion.STRETCH,
        "nod": Motion.NOD,
        "head_shake": Motion.HEAD_SHAKE,
        "shake_head": Motion.HEAD_SHAKE,
    }

    # Which leg a wave motion name waves (3 = front-left, 6 = front-right);
    # bare "wave" defaults to the right. Shared by set_motion() and _coerce_step()
    # so the string and choreography APIs always pick the same side.
    _WAVE_LEG_FOR: ClassVar[dict[str, int]] = {
        "wave": 6,
        "wave_right": 6,
        "wave_left": 3,
    }

    def __init__(
        self,
        gait_speed: float = 0.012,
        step_length: float = 30.0,
        step_height: float = 30.0,
        on_step: Callable[[dict[str, int]], None] | None = None,
        *,
        fps: int = 60,
        driver: ServoDriver | None = None,
        display: FaceDisplay | None = None,
        speaker: Speaker | None = None,
        camera: HeadCamera | None = None,
        mic: Microphone | MicStream | None = None,
        auto_wake: bool = True,
    ):
        if fps <= 0:
            raise ValueError("fps must be positive.")
        self._engine = MotionEngine(
            gait_speed=gait_speed,
            step_length=step_length,
            step_height=step_height,
            dt=1.0 / fps,  # keep the second-based posture one-shots wall-clock-true
        )
        self._driver = driver
        self._display = display
        self._speaker = speaker
        self._camera = camera
        self._mic = mic
        self._on_step = on_step
        self._fps = fps
        self._auto_wake = auto_wake
        # Which gesture's servo tuning is currently applied:
        #   ("wave", legs_tuple, arm_iir_alpha) | ("stretch",) | None.
        # One shared state so gesture-to-gesture switches release the old
        # tuning before applying the new (see _sync_gesture_tuning). The
        # arm_iir alpha is part of the wave key because wave_both and clap
        # share the same arm pair but need different IIR weights — switching
        # between them must re-apply.
        self._gesture_tuned: tuple | None = None
        # Whether the neck-gesture Profile_Velocity tuning is currently applied
        # (same enter/exit tracking as the gesture tuning, for nod / head-shake).
        self._neck_gesture_tuned: bool = False
        # Cross-thread cancel signal for run() (see cancel() / MotionCancelled).
        # A monotonically increasing counter guarded by a small lock, NOT a
        # threading.Event that gets clear()ed: each paced public method
        # (run() / perform_dance() / play_realtime()) snapshots the counter
        # the instant it is entered, and every frame boundary compares the
        # LIVE counter against that snapshot. An Event-based design has to
        # clear() itself at the pacing loop's own entry to stop a stale
        # cancel() bleeding into the next call -- and that clear() can race a
        # concurrent set() from another thread and silently lose it. The
        # counter has no clear() step to race against, so that window cannot
        # occur; see cancel()'s docstring for the exact contract this gives.
        self._cancel_count: int = 0
        self._cancel_lock = threading.Lock()
        # Set by _arm_cancel_scope() / cleared by _disarm_cancel_scope() (both
        # under _cancel_lock) -- see their docstrings. Lets a caller that is
        # about to dispatch a paced call onto another thread (e.g.
        # AgentToolSet.call) close the residual window described in
        # cancel()'s docstring: a cancel() delivered after dispatch but
        # before the worker thread actually reaches run()/perform_dance()/
        # play_realtime() would otherwise be silently absorbed by that
        # method's own entry snapshot.
        self._armed_snapshot: int | None = None

    @property
    def fps(self) -> int:
        """Control rate in cycles per second."""
        return self._fps

    @property
    def dt(self) -> float:
        """Seconds per control cycle (``1 / fps``)."""
        return 1.0 / self._fps

    # ================================================================
    # CONNECTION LIFECYCLE
    # ================================================================

    @property
    def driver(self) -> ServoDriver | None:
        """The attached servo driver, or ``None`` for compute-only mode."""
        return self._driver

    @property
    def display(self) -> FaceDisplay | None:
        """The attached face display, or ``None`` when no face is wired."""
        return self._display

    @property
    def speaker(self) -> Speaker | None:
        """The attached text-to-speech speaker, or ``None`` when none is wired."""
        return self._speaker

    @property
    def camera(self) -> HeadCamera | None:
        """The attached head camera, or ``None`` when no camera is wired."""
        return self._camera

    @property
    def mic(self) -> Microphone | MicStream | None:
        """The attached microphone (or MicStream), or ``None`` when no mic is wired."""
        return self._mic

    @property
    def has_connectable_resource(self) -> bool:
        """Whether any resource (driver / display / speaker / camera / mic) is attached.

        ``connect()`` needs at least one; a facade with them all ``None`` is
        compute-only and has nothing to open. Callers that own the facade use
        this to decide whether to run the connect/disconnect ceremony at all,
        rather than re-deriving the ``None`` check themselves.
        """
        return (
            self._driver is not None
            or self._display is not None
            or self._speaker is not None
            or self._camera is not None
            or self._mic is not None
        )

    @property
    def is_connected(self) -> bool:
        """Whether a driver is attached and currently connected."""
        return self._driver is not None and self._driver.is_connected

    def connect(self) -> Palmimo:
        """Open the driver, face-display, speaker, camera, and/or mic. Returns ``self``.

        Connects whichever resources are attached: the driver (servo bus), the
        display (which also plays its boot-wake animation, mirroring what a real
        startup shows), the speaker (a piper availability probe), the camera
        (opens the head camera's ``cv2.VideoCapture`` and starts its background
        frame drain), and the mic. A facade with only a subset of these (e.g.
        ``driver=None``) is valid — a face, voice, ears, and/or camera with no legs.

        When a driver is attached and connected, and *auto_wake* was not
        disabled at construction, :meth:`wake` runs automatically once every
        resource has opened — the robot glides from limp to neutral instead of
        sitting stiff/limp until a caller remembers to wake it. A wake failure
        rolls back every resource already opened, same as any other connect
        failure. :meth:`wake` already no-ops without a connected driver, so
        compute-only facades (and ``auto_wake=False``) are unaffected. Rollback
        also runs on a ``KeyboardInterrupt`` during the wake glide, and
        soft-releases the neck before the driver disconnect cuts torque.

        Raises:
            RuntimeError: If no driver, display, speaker, camera, or mic was attached at
                construction.
        """
        if not self.has_connectable_resource:
            raise RuntimeError(
                "Nothing to connect: no driver, display, speaker, camera, or mic attached. "
                "Pass a ServoDriver via Palmimo(driver=...), a FaceDisplay via "
                "Palmimo(display=...), a Speaker via Palmimo(speaker=...), a "
                "HeadCamera via Palmimo(camera=...), and/or a Microphone via Palmimo(mic=...)."
            )
        # Roll back on partial failure: if a later resource fails to open, close
        # the ones already opened before re-raising. Otherwise a raise here leaks
        # them — via ``with`` a failing __enter__ means __exit__ never runs, and
        # the plain connect() path would leave earlier resources open too. Rollback
        # is best-effort + suppressed so it never masks the original error.
        driver_connected = False
        try:
            if self._driver is not None:
                self._driver.connect()
                driver_connected = True
            if self._display is not None:
                self._display.connect()
                self._display.wake()
            if self._speaker is not None:
                self._speaker.open()
            if self._camera is not None:
                self._camera.open()
                self._camera.start_drain()
            if self._mic is not None:
                self._mic.open()
            # A fresh driver connect() re-applies the driver's default RAM tuning
            # (PV/gain), so any per-axis tweak tracked from a previous connection
            # is gone — the tracking must not survive a reconnect or the re-apply
            # would be skipped as "already tuned".
            self._gesture_tuned = None
            self._neck_gesture_tuned = False
            # Inside the try block so a wake failure rolls back every resource
            # already opened (same as any other connect failure) instead of
            # leaving the robot half-connected.
            if self._auto_wake:
                self.wake()
        except BaseException:
            # BaseException, not Exception: a Ctrl+C during the ~1.5s wake glide
            # arrives as KeyboardInterrupt, which is NOT an Exception subclass —
            # narrower catches used to let it skip rollback entirely, leaking
            # every resource already opened with torque still on.
            if driver_connected and self._driver is not None:
                # Wake may have left the neck held at full gain; soft-release it
                # BEFORE the driver.disconnect() below cuts torque, or the head
                # snaps down — the exact hazard the park exists to prevent.
                # _park_neck() itself absorbs a further Ctrl+C.
                with contextlib.suppress(Exception):
                    self._park_neck()
            if self._mic is not None:
                with contextlib.suppress(Exception):
                    self._mic.close()  # idempotent even if open never landed
            if self._camera is not None:
                with contextlib.suppress(Exception):
                    self._camera.close()  # idempotent even if open never landed
            if self._speaker is not None:
                with contextlib.suppress(Exception):
                    self._speaker.close()  # idempotent even if open never landed
            if self._display is not None:
                with contextlib.suppress(Exception):
                    self._display.disconnect()  # idempotent even if connect never landed
            if driver_connected and self._driver is not None:
                with contextlib.suppress(Exception):
                    self._driver.disconnect()
            raise  # re-raises KeyboardInterrupt too
        return self

    def disconnect(self, *, park: bool = True) -> None:
        """Park the robot, then close the mic, camera, speaker, face-display, and/or driver. Safe when idle.

        Parks first (skipped when idle): with *park* true, eases the legs to
        neutral via :meth:`return_to_neutral`, then, regardless of *park*,
        soft-releases the neck via :meth:`_park_neck` — this is a
        gain-lowering ramp, not a motion command, so it is safe (and
        desirable) even right after an exception: it stops the head dropping
        on torque-off. Pass ``park=False`` to skip only the leg return — used
        by :meth:`__exit__` after an exception, where streaming more motion
        commands could mask the error / be unsafe on a robot that may already
        be in a bad state.

        Then closes the mic, then the camera (which stops its background frame
        drain before releasing ``cv2.VideoCapture`` — see
        :meth:`~palmimo_sdk.io.camera.HeadCamera.close`), closes the speaker,
        returns the display to its neutral IDLE face and closes it, then closes
        the driver. Delegates to each unconditionally (the resource
        ``close``/``disconnect``\\ s are idempotent); we must not gate on
        ``is_connected`` here, or a half-open resource (e.g. USB yanked) would
        never get cleaned up.

        The whole body — park, every peripheral close, and the driver
        disconnect that actually cuts torque — runs under one
        :func:`_deferred_interrupts` block: each peripheral close is wrapped
        in its own ``suppress(Exception)`` so a flaky one never blocks the
        rest, but that does NOT catch :class:`KeyboardInterrupt`, so a Ctrl+C
        landing anywhere in the sequence — including mid-``camera.close()``'s
        frame drain — would otherwise escape and skip the torque-off. The
        inner ``suppress(Exception, KeyboardInterrupt)`` around the leg return
        is the fallback for when :func:`_deferred_interrupts` is a no-op (off
        the main thread, or on an interpreter that refuses handler
        installation): in that case the leg return must still fall through to
        the neck release on its own.

        On a connected robot this takes ~2.5s+ (the 14-step neck release ramp
        alone is ~2.5s at :data:`_NECK_RELEASE_STEP_S`; ``park=True`` adds the
        leg return on top) — callers on a tight shutdown budget (signal
        handlers, service stop timeouts) must allow for it, since a hard kill
        mid-ramp cuts torque at a low gain.
        """
        with _deferred_interrupts():
            if self.is_connected:
                if park:
                    with contextlib.suppress(Exception, KeyboardInterrupt):
                        self.return_to_neutral()
                with contextlib.suppress(Exception):
                    self._park_neck()
            if self._mic is not None:
                with contextlib.suppress(Exception):
                    self._mic.close()
            if self._camera is not None:
                with contextlib.suppress(Exception):
                    self._camera.close()
            if self._speaker is not None:
                with contextlib.suppress(Exception):
                    self._speaker.close()
            if self._display is not None:
                with contextlib.suppress(Exception):
                    self._display.idle()
                with contextlib.suppress(Exception):
                    self._display.disconnect()
            if self._driver is not None:
                self._driver.disconnect()
        # Per-axis tuning tracking is connection-scoped (see connect()).
        self._gesture_tuned = None
        self._neck_gesture_tuned = False

    def return_to_neutral(
        self,
        max_steps: int = 120,
        settle_ticks: int = 2,
        *,
        duration: float | None = None,
        _cancel_snapshot: int | None = None,
    ) -> None:
        """Bring all servos back to neutral.

        With *duration* (seconds), perform a software glide: read current
        positions and interpolate every joint to center over *duration* with a
        raised cosine, writing each frame at the control rate. This needs a
        connected driver that supports ``read_positions`` and — unlike a
        profile-velocity glide — never snaps if the bus is flaky, giving the
        smooth, controlled return real hardware wants.

        Without *duration* (the default), ease back by advancing control cycles
        until every joint has settled within *settle_ticks* of center, or
        *max_steps* is reached (a safety cap). Looping to convergence — rather
        than a fixed step count — keeps this robust to changes in the engine's
        idle-smoothing rate. This path works compute-only (no driver needed).

        "Neutral" here is per-joint: raw servo neutral everywhere except the
        neck pitch, whose front is the engine's rest-trimmed center (see
        :meth:`MotionEngine.neck_pitch_center`).

        *_cancel_snapshot* is internal — used only by :meth:`perform_dance` to
        make its own glide-home cancellable via the counter+snapshot scheme
        documented on :meth:`cancel`. ``None`` (the default, and every public
        caller) means this call is not cancellable, matching this method's
        long-standing behavior.
        """
        # Validate before stop() so a bad duration can't leave the engine idled.
        if duration is not None and duration <= 0:
            raise ValueError("duration must be positive.")
        self.stop()
        # Motion is now IDLE — release any gesture / neck-gesture tuning before
        # the glide (the timed path drives the driver directly and never calls
        # step()).
        self._sync_gesture_tuning()
        self._sync_neck_gesture_tuning()
        if duration is not None:
            self._timed_return_to_neutral(duration, _cancel_snapshot)
            return
        targets = self._neutral_targets()
        for _ in range(max_steps):
            if _cancel_snapshot is not None:
                self._check_cancelled(_cancel_snapshot)
            pos = self.step()
            if all(abs(tick - targets[name]) <= settle_ticks for name, tick in pos.items()):
                break

    def _neutral_targets(self) -> dict[str, int]:
        """Per-joint neutral pose: raw neutral except the rest-trimmed neck pitch."""
        targets = dict.fromkeys(self._engine.get_positions(), MotionEngine.NEUTRAL)
        targets["neck_pitch1"] = self._engine.neck_pitch_center()
        return targets

    def wake(self, duration: float = 1.5, start_gain: int = _WAKE_START_GAIN) -> None:
        """Wake from limp: glide from the current pose to neutral while ramping
        Position_P_Gain from *start_gain* up to the default, so the robot softly
        firms up and rises instead of snapping to a stiff neutral.

        :meth:`connect` already runs this automatically once every resource is
        open (unless the facade was built with ``auto_wake=False``); call it
        directly only for a custom *duration* / *start_gain*, or to re-wake
        without a full reconnect.

        Falls back to a plain glide if the driver can't ramp gain, and to a single
        neutral command if it can't sense position.
        """
        driver = self._driver
        if driver is None or not driver.is_connected:
            return
        if duration <= 0:
            raise ValueError("duration must be positive.")
        self.stop()
        targets = self._neutral_targets()
        try:
            current = driver.read_positions()
        except NotImplementedError:
            current = {}  # driver can't sense position — fall back to a single neutral command
        if not current:
            driver.write_positions(targets)
            self._seed_engine_pose(targets)
            return
        has_gain = hasattr(driver, "set_position_p_gain")
        # Only the legs ramp up from the soft start_gain — the neck must stay
        # firm the whole time, or the head sags the moment we soften it (the
        # neck can't hold the head's weight at low P-gain). Keeping the neck at
        # full gain is what stops the "drop on wake" on a repeated run where the
        # head was already held up at neutral.
        legs = [n for n in current if n not in _NECK_MOTORS]
        neck = [n for n in current if n in _NECK_MOTORS]
        if has_gain and neck:
            with contextlib.suppress(Exception):
                driver.set_position_p_gain(_WAKE_FULL_GAIN, motors=neck)
        steps = max(1, round(duration * self._fps))
        for s in range(1, steps + 1):
            f = 0.5 * (1.0 - math.cos(math.pi * s / steps))  # 0 -> 1, eased
            if has_gain:
                try:
                    driver.set_position_p_gain(int(start_gain + (_WAKE_FULL_GAIN - start_gain) * f), motors=legs)
                except Exception:
                    has_gain = False  # driver can't ramp gain — keep gliding
            frame = {n: round(p + (targets.get(n, MotionEngine.NEUTRAL) - p) * f) for n, p in current.items()}
            driver.write_positions(frame)
            time.sleep(1.0 / self._fps)
        if has_gain:
            with contextlib.suppress(Exception):
                driver.set_position_p_gain(None)  # land exactly on the default gain
        self._seed_engine_pose(targets)

    def sleep(self, duration: float = 1.5, end_gain: int = _WAKE_START_GAIN) -> None:
        """Settle to neutral and go limp, the inverse of :meth:`wake`.

        Glides the legs to the neutral pose while ramping their
        Position_P_Gain *down* to *end_gain* -- which defaults to where
        :meth:`wake` starts, so the two meet -- then soft-releases the neck so
        the head eases down instead of staying held up. The robot ends
        compliant rather than dead: torque stays on at *end_gain*, which is
        what makes :meth:`wake` reversible. The default is the only value the
        legs have been observed to hold the body at; below it they are on their
        own weight, so *end_gain* must be positive and no higher than the
        default running gain.

        What the neck ends up **commanded** to is the delicate part, because
        :meth:`_park_neck` then walks its gain down until the head rests on the
        top plate. A goal left where the head no longer is becomes a stored
        error, and the next write that raises the gain -- :meth:`wake`, a wave's
        global tuning, :meth:`set_p_gain` -- releases it as a jolt. So the glide
        holds the neck where it already is, and once the head has settled its
        goal is rewritten to the pose it actually reached. That second write
        moves nothing (the gain is ~0 by then); it exists to leave the error at
        zero for whoever raises the gain next.

        Peripherals are deliberately untouched. Unlike :meth:`disconnect`, the
        mic and camera stay open, so a sleeping robot can still be spoken to.

        Degrades rather than skipping the point: a driver that can't sense
        position still gets the gain ramp and the neck release (only the glide
        needs feedback), and one that can't ramp gain still gets the glide.
        """
        driver = self._driver
        if driver is None or not driver.is_connected:
            return
        if duration <= 0:
            raise ValueError("duration must be positive.")
        if not 0 < end_gain <= _WAKE_FULL_GAIN:
            raise ValueError(f"end_gain must be in (0, {_WAKE_FULL_GAIN}]; got {end_gain}.")
        self.stop()
        # Motion is now IDLE — release any gesture / neck-gesture tuning before
        # the glide, exactly as return_to_neutral does: this path drives the
        # driver directly and never calls step(), so nothing else would. Left
        # applied, _gesture_tuned also stays stale, and the first step() after
        # waking restores the default gain on a neck this method just walked
        # down to limp.
        self._sync_gesture_tuning()
        self._sync_neck_gesture_tuning()
        targets = self._neutral_targets()
        try:
            current = driver.read_positions()
        except NotImplementedError:
            current = {}  # driver can't sense position — no glide, but everything else still runs
        has_gain = hasattr(driver, "set_position_p_gain")
        # Legs only, exactly as wake() does: the neck is softened afterwards by
        # _park_neck's own ramp, which is shaped to let the head down gently.
        # Softening it on this ramp instead would drop the head mid-glide.
        # Named from the neutral targets, not from the sensed pose, so the ramp
        # is still legs-only when position feedback is unavailable. Ramping the
        # neck down here too would leave _park_neck's ladder starting *above*
        # it, jolting the head up before letting it down.
        legs = [n for n in targets if n not in _NECK_MOTORS]
        start_gain = self._present_leg_gain(legs)
        # Every frame carries the neck at its present pose rather than easing it
        # to neutral. Dropping the keys instead would not leave it alone: a
        # driver backfills an unnamed motor with NEUTRAL, which is the head-up
        # goal this is here to avoid, arrived at in one step instead of a glide.
        steps = max(1, round(duration * self._fps))
        landed = dict(current)
        try:
            for s in range(1, steps + 1):
                f = 0.5 * (1.0 - math.cos(math.pi * s / steps))  # 0 -> 1, eased
                if has_gain:
                    try:
                        driver.set_position_p_gain(int(start_gain + (end_gain - start_gain) * f), motors=legs)
                    except Exception:
                        has_gain = False  # driver can't ramp gain — keep gliding
                if not current:
                    continue  # no feedback to interpolate from; the ramp above is the whole job
                frame = {
                    n: (round(p + (targets.get(n, MotionEngine.NEUTRAL) - p) * f) if n in legs else p)
                    for n, p in current.items()
                }
                driver.write_positions(frame)
                # Only after the write returns: sync_write raises with nothing
                # applied, so a frame that failed is not a pose the robot reached.
                landed = frame
                time.sleep(1.0 / self._fps)
        finally:
            # However the loop ended. Each step is suppressed separately: this
            # runs on the EIO path it exists for, where the gain writes are
            # failing too, and an exception raised here would replace the
            # original one *and* skip the soft release.
            self._seed_engine_pose(landed)
            with contextlib.suppress(Exception):
                self._park_neck()
            with contextlib.suppress(Exception):
                self._settle_neck_goal(driver, landed)

    def _present_leg_gain(self, legs: list[str] | None) -> int:
        """Where the leg gain ramp should start: whatever the legs are at now.

        Assuming the default would snap the body up before walking it down --
        a second :meth:`sleep`, or one after :meth:`set_p_gain`, starts from a
        low gain with the legs already sagged under the body's weight. Falls
        back to the default gain when the driver cannot read it back.
        """
        driver = self._driver
        if driver is None or not hasattr(driver, "read_position_p_gain"):
            return _WAKE_FULL_GAIN
        try:
            live = driver.read_position_p_gain()
        except Exception:
            return _WAKE_FULL_GAIN
        wanted = [v for n, v in live.items() if legs is None or n in legs]
        if not wanted:
            return _WAKE_FULL_GAIN
        # The highest of them: starting below where a leg actually sits would
        # drop that leg at the first write of the ramp. Bounded by the default
        # so a stray reading cannot make the ramp start above running stiffness.
        return min(max(wanted), _WAKE_FULL_GAIN)

    def _settle_neck_goal(self, driver: ServoDriver, base: dict[str, int]) -> None:
        """Rewrite the neck's goal to the pose the head actually came to rest at.

        :meth:`_park_neck` lets the head down onto the top plate, which leaves
        the goal it was holding metres away in tick terms. Nothing acts on that
        while the gain is ~0 -- but :meth:`wake` raises the neck gain *before*
        it writes a position, and a wave's tuning raises every motor's gain at
        once, so the stored error comes out as a jolt at the worst moment. A
        write here moves nothing and costs one frame; it just makes the error
        zero for whoever raises the gain next.
        """
        if not base:
            return  # nothing was glided, so there is no leg goal to preserve
        settled = driver.read_positions()
        neck = {n: p for n, p in settled.items() if n in _NECK_MOTORS}
        if not neck:
            return
        # Carries *base* -- the last frame the glide landed -- alongside the
        # neck, because write_positions backfills an unnamed motor with NEUTRAL:
        # writing the neck alone would re-command every leg.
        driver.write_positions({**base, **neck})
        self._engine.set_neck_positions(neck)

    def _timed_return_to_neutral(self, duration: float, cancel_snapshot: int | None = None) -> None:
        if self._driver is None or not self._driver.is_connected:
            raise RuntimeError("Timed return-to-neutral needs a connected driver.")
        targets = self._neutral_targets()
        current = self._driver.read_positions()
        if not current:
            # No position feedback (read failed) — can't interpolate a glide, so
            # just command the neutral pose once rather than risk a
            # profile-velocity snap.
            self._driver.write_positions(targets)
            self._seed_engine_pose(targets)
            return
        # Software glide: interpolate every joint from its current tick to its
        # neutral target over *duration*, writing each frame at the control
        # rate. This eases in via raised cosine and — unlike a profile-velocity
        # glide — never snaps if the bus is flaky, mirroring the current-survey
        # scripts' glide_to.
        steps = max(1, round(duration * self._fps))
        for s in range(1, steps + 1):
            if cancel_snapshot is not None:
                self._check_cancelled(cancel_snapshot)
            f = 0.5 * (1.0 - math.cos(math.pi * s / steps))  # 0 -> 1, eased
            frame = {n: round(p + (targets.get(n, MotionEngine.NEUTRAL) - p) * f) for n, p in current.items()}
            self._driver.write_positions(frame)
            time.sleep(1.0 / self._fps)
        # Seed the engine from the real pose so the first motion interpolates
        # from it instead of stale internal state.
        self._seed_engine_pose(targets)

    def _seed_engine_pose(self, positions: dict[str, int]) -> None:
        """Seed the engine's leg AND neck state after driving servos directly.

        Legs alone are not enough: a neck gesture captures its blend start
        from the engine's neck state, so a stale neck (e.g. after the timed
        neutral glide moved the head on hardware) would make the first
        gesture snap by the rest-trim offset before blending back.
        """
        self._engine.set_leg_positions(positions)
        self._engine.set_neck_positions(positions)

    def _park_neck(self) -> None:
        """Soft-release the neck so torque-off doesn't jolt the head.

        Steps the neck's Position_P_Gain down so the head eases from its held pose
        to a near-limp hold; the disconnect then cuts torque with almost no stored
        holding force to release (measured release jolt ~0 ticks). Adaptive — no
        fixed rest pose to maintain. Best-effort: skipped if the driver can't set
        per-motor gains. Runs under :func:`_deferred_interrupts`, so a shutdown
        Ctrl+C — however many times it is pressed — can neither abort the ramp
        nor rush it through its dwells.

        Leaves the gain low for whatever follows, and does not restore it —
        :meth:`connect` re-applies the default gain, :meth:`wake` ramps back up.
        Callers that stay live afterwards (:meth:`sleep`) must have left the
        neck's goal position at the pose it is actually holding: the stored
        error a mismatched goal leaves is what the next gain write snaps the
        head up out of. On the :meth:`disconnect` path torque is cut straight
        after, so nothing is left to act on it.
        """
        driver = self._driver
        if driver is None or not driver.is_connected or not hasattr(driver, "set_position_p_gain"):
            return
        neck = list(_NECK_MOTORS)
        with _deferred_interrupts():
            for gain in _NECK_RELEASE_GAINS:
                try:
                    driver.set_position_p_gain(gain, motors=neck)
                    time.sleep(_NECK_RELEASE_STEP_S)
                except (NotImplementedError, TypeError):
                    return  # driver can't set per-motor gains (no capability or no motors=) — skip
                except KeyboardInterrupt:
                    continue  # unreachable while the guard above holds; fallback for its documented no-op case

    def __enter__(self) -> Palmimo:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # disconnect() parks before closing: a clean exit parks fully (ease to
        # neutral, then soft-release the neck); after an exception only the
        # neck soft-release runs (park=False skips the leg return — streaming
        # more motion commands after an exception could mask it / be unsafe).
        # Best-effort: a disconnect error must never replace the original
        # exception propagating out of the with-block.
        with contextlib.suppress(Exception):
            self.disconnect(park=exc_type is None)

    # ================================================================
    # MOTION COMMANDS
    # ================================================================

    def forward(self) -> None:
        """Start walking forward."""
        self._engine.motion = Motion.FORWARD

    def backward(self) -> None:
        """Start walking backward."""
        self._engine.motion = Motion.BACKWARD

    def strafe_left(self) -> None:
        """Start strafing left."""
        self._engine.motion = Motion.STRAFE_LEFT

    def strafe_right(self) -> None:
        """Start strafing right."""
        self._engine.motion = Motion.STRAFE_RIGHT

    def rotate_left(self) -> None:
        """Rotate body counter-clockwise (viewed from above)."""
        self._engine.motion = Motion.ROTATE_LEFT

    def rotate_right(self) -> None:
        """Rotate body clockwise (viewed from above)."""
        self._engine.motion = Motion.ROTATE_RIGHT

    def dance(self) -> None:
        """Start the dance motion (body sway)."""
        self._engine.motion = Motion.DANCE

    def body_tilt(self) -> None:
        """Tilt the body side to side (curious expression)."""
        self._engine.motion = Motion.BODY_TILT

    def pushup(self) -> None:
        """Do push-ups (raise and lower the body)."""
        self._engine.motion = Motion.PUSHUP

    def wave(self) -> None:
        """Wave the front-right leg (greeting gesture)."""
        # Pin the leg so a bare wave() is always front-right, even right after a
        # wave_left() / play(("wave_left", …)) left wave_leg on the left.
        self._engine.wave_leg = self._WAVE_LEG_FOR["wave"]
        self._engine.motion = Motion.WAVE

    def wave_both(self) -> None:
        """Wave BOTH front legs at once (two-handed greeting).

        Builds the "beg" stance first (middle feet forward = tip margin, small
        lean back, nose-up) so the mid/rear quad keeps the robot stable while
        both front legs lift — see :meth:`MotionEngine._apply_wave_both`.
        Defaults are the analyzed stable zone; open up via the ``wave_both_*``
        knobs after checking tip margin on hardware.
        """
        self._engine.motion = Motion.WAVE_BOTH

    def clap(self) -> None:
        """Clap the two front feet together (never touching) on the beg stance.

        Hands rise to ``clap_height`` and swing together/apart ``clap_count``
        times; the closest approach is ``clap_gap`` mm between foot centres —
        see :meth:`MotionEngine._apply_clap`. Shares the beg-stance knobs
        (``wave_both_lean`` / ``wave_both_noseup`` / ``wave_both_mid_forward``)
        with the two-handed wave.
        """
        self._engine.motion = Motion.CLAP

    def creep(self) -> None:
        """Slow creep gait (one leg at a time, very stable)."""
        self._engine.motion = Motion.CREEP

    def bow(self) -> None:
        """Bow once (greeting gesture): chest and head dip, hold, slow rise."""
        self._engine.motion = Motion.BOW

    def stretch(self) -> None:
        """Stretch once: wind-up crouch, rise tall on folded femurs, lower."""
        self._engine.motion = Motion.STRETCH

    def nod(self) -> None:
        """Nod "yes" — the chin dips and returns twice (neck-only one-shot)."""
        self._engine.motion = Motion.NOD

    def head_shake(self) -> None:
        """Shake the head "no" — sideways swings that taper off (neck-only one-shot)."""
        self._engine.motion = Motion.HEAD_SHAKE

    def stop(self) -> None:
        """Stop all motion and smoothly return to neutral."""
        self._engine.motion = Motion.IDLE

    def cancel(self) -> None:
        """Signal an in-flight :meth:`run` (or :meth:`perform_dance` /
        :meth:`play_realtime`) to abort at the next frame boundary, raising
        :class:`MotionCancelled` there.

        **The one facade method safe to call from a thread other than the
        one driving the robot** (the thread inside ``run()``/``step()``): it
        only increments a counter under a small lock and touches no other
        Palmimo state, so it cannot race the motion loop's own reads/writes.

        Contract: a ``cancel()`` delivered at any point between a paced
        method's entry and its return is guaranteed to raise
        :class:`MotionCancelled` inside that call; one delivered strictly
        before entry never carries over into it (each paced method snapshots
        the counter at entry and frame boundaries compare against that
        snapshot). A caller that dispatches the paced call onto ANOTHER
        thread has one residual window — a ``cancel()`` landing after
        dispatch but before the paced method's entry is ignored — closable
        via :meth:`_arm_cancel_scope` / :meth:`_disarm_cancel_scope` around
        the dispatch, which
        :meth:`~palmimo_sdk.agent.toolset.AgentToolSet.call` already does;
        a direct dispatcher that doesn't arm should treat its own cancel
        request as the authoritative outcome for reporting.
        """
        with self._cancel_lock:
            self._cancel_count += 1

    def _arm_cancel_scope(self) -> None:
        """Record the live cancel count as the "armed" baseline for the NEXT
        paced call's entry snapshot, closing the dispatch-to-entry cancel
        window described in :meth:`cancel`'s docstring.

        Intended for a caller that is about to hand a paced call (``run()`` /
        ``perform_dance()`` / ``play_realtime()``) off to another thread --
        e.g. :meth:`~palmimo_sdk.agent.toolset.AgentToolSet.call` right before
        ``asyncio.to_thread``-dispatching a tool's ``execute()``. Call this
        the instant before dispatch; a ``cancel()`` delivered anywhere from
        this call onward -- including before the worker thread has actually
        reached its paced method's own entry -- is then guaranteed to raise
        :class:`MotionCancelled` once that method starts pacing, because the
        armed value becomes that method's OWN entry snapshot (see
        :meth:`_take_cancel_snapshot`), not a fresh, later read of the live
        counter.

        Always pair with :meth:`_disarm_cancel_scope` in a ``finally``, or an
        armed-but-never-consumed scope (a tool that never reaches a paced
        call) leaks into whatever the NEXT dispatch does.
        """
        with self._cancel_lock:
            self._armed_snapshot = self._cancel_count

    def _disarm_cancel_scope(self) -> None:
        """Clear an armed scope set by :meth:`_arm_cancel_scope`.

        Idempotent, and safe to call even if the armed value was already
        consumed by :meth:`_take_cancel_snapshot` (it is simply reset to
        ``None`` again). Callers pair this with :meth:`_arm_cancel_scope` in
        a ``finally`` so an armed scope never bleeds into a later, unrelated
        dispatch.
        """
        with self._cancel_lock:
            self._armed_snapshot = None

    def _take_cancel_snapshot(self) -> int:
        """The entry snapshot a paced public method (``run()`` /
        ``perform_dance()`` / ``play_realtime()``) should take at its own
        entry, consuming an armed scope if one is pending.

        Under :attr:`_cancel_lock`: if :meth:`_arm_cancel_scope` armed a
        baseline that hasn't been consumed yet, this returns THAT value
        (and resets the armed slot to ``None`` -- an armed scope is consumed
        exactly once, by whichever paced call reaches entry first) instead
        of a fresh read of the live counter. Returning the (earlier) armed
        value rather than the live count is what makes a ``cancel()``
        delivered between arming and this call's entry count as "after
        entry" -- closing the window :meth:`cancel`'s docstring documents.
        With no armed scope pending, this is just ``self._cancel_count``,
        identical to every paced method's previous, unarmed behavior.
        """
        with self._cancel_lock:
            if self._armed_snapshot is not None:
                snapshot = self._armed_snapshot
                self._armed_snapshot = None
                return snapshot
            return self._cancel_count

    def _check_cancelled(self, cancel_snapshot: int) -> None:
        """Raise :class:`MotionCancelled` if :meth:`cancel` ran since *cancel_snapshot* was taken."""
        if self._cancel_count > cancel_snapshot:
            raise MotionCancelled("Palmimo.cancel() was called; motion aborted mid-run().")

    def cancel_checkpoint(self) -> int:
        """Entry point for a hand-rolled control loop (one that paces its own
        ``step()`` / ``run(steps=1)`` calls) to start participating in the
        :meth:`cancel` contract. Call once, at the top of the loop, and pass
        the returned value to :meth:`raise_if_cancelled` on every iteration.

        Delegates to :meth:`_take_cancel_snapshot`, so it follows the exact
        same rule every paced public method uses (and consumes an armed
        scope from :meth:`_arm_cancel_scope` the same way).
        """
        return self._take_cancel_snapshot()

    def raise_if_cancelled(self, checkpoint: int) -> None:
        """Raise :class:`MotionCancelled` if :meth:`cancel` ran since *checkpoint* (from :meth:`cancel_checkpoint`)."""
        self._check_cancelled(checkpoint)

    def _sleep_cancellable(self, seconds: float, cancel_snapshot: int) -> None:
        """Sleep *seconds*, but wake early and raise :class:`MotionCancelled` if cancelled.

        Used by :meth:`perform_dance`'s non-paced hold/glide sections so a
        ``cancel()`` lands within roughly one poll interval rather than only
        once the whole sleep finishes -- the same responsiveness
        :meth:`_pace` gives the paced sections.
        """
        deadline = time.perf_counter() + seconds
        while True:
            self._check_cancelled(cancel_snapshot)
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return
            time.sleep(min(_CANCEL_POLL_INTERVAL_S, remaining))

    def set_motion(self, name: str) -> None:
        """Set motion by string name.

        Args:
            name (str): A motion name accepted by the facade — see ``_MOTION_MAP`` for the
                full set (includes aliases like ``"tilt"`` / ``"push_up"`` /
                ``"stop"``). Case-insensitive; surrounding whitespace is stripped.

        Raises:
            ValueError: If *name* is not a recognized motion. The message lists the
                available names.
        """
        key = name.lower().strip()
        if key not in self._MOTION_MAP:
            available = ", ".join(sorted(self._MOTION_MAP.keys()))
            raise ValueError(f"Unknown motion {name!r}. Choose from: {available}")
        if key in self._WAVE_LEG_FOR:
            self._engine.wave_leg = self._WAVE_LEG_FOR[key]
        self._engine.motion = self._MOTION_MAP[key]

    # Wave tuning knobs (all runtime-settable). Descriptions, defaults and the
    # automatic per-axis servo tuning are documented in
    # doc/reference/api-reference.md › "Wave Gesture Tuning" — the single source of truth.
    # Kept out of these docstrings so the same text isn't maintained twice.
    @property
    def wave_speed(self) -> float:
        return self._engine.wave_speed

    @wave_speed.setter
    def wave_speed(self, value: float) -> None:
        self._engine.wave_speed = float(value)

    @property
    def wave_intro_speed(self) -> float:
        return self._engine.wave_intro_speed

    @wave_intro_speed.setter
    def wave_intro_speed(self, value: float) -> None:
        self._engine.wave_intro_speed = float(value)

    @property
    def wave_raise_speed(self) -> float:
        return self._engine.wave_raise_speed

    @wave_raise_speed.setter
    def wave_raise_speed(self, value: float) -> None:
        self._engine.wave_raise_speed = float(value)

    @property
    def wave_size(self) -> float:
        return self._engine.wave_size

    @wave_size.setter
    def wave_size(self, value: float) -> None:
        self._engine.wave_size = float(value)

    @property
    def wave_decay(self) -> float:
        return self._engine.wave_decay

    @wave_decay.setter
    def wave_decay(self, value: float) -> None:
        self._engine.wave_decay = float(value)

    @property
    def wave_count(self) -> int:
        return self._engine.wave_count

    @wave_count.setter
    def wave_count(self, value: int) -> None:
        self._engine.wave_count = int(value)

    @property
    def wave_period(self) -> float:
        return self._engine.wave_period

    @wave_period.setter
    def wave_period(self, value: float) -> None:
        self._engine.wave_period = float(value)

    @property
    def wave_dwell(self) -> float:
        return self._engine.wave_dwell

    @wave_dwell.setter
    def wave_dwell(self, value: float) -> None:
        self._engine.wave_dwell = float(value)

    # Nod / head-shake tuning knobs (all runtime-settable). Documented in
    # doc/reference/api-reference.md › "Nod / Head-Shake Gesture Tuning" — the single
    # source of truth, same policy as the wave knobs above.
    @property
    def nod_amp_deg(self) -> float:
        return self._engine.nod_amp_deg

    @nod_amp_deg.setter
    def nod_amp_deg(self, value: float) -> None:
        self._engine.nod_amp_deg = float(value)

    @property
    def nod_period(self) -> float:
        return self._engine.nod_period

    @nod_period.setter
    def nod_period(self, value: float) -> None:
        self._engine.nod_period = float(value)

    @property
    def nod_count(self) -> int:
        return self._engine.nod_count

    @nod_count.setter
    def nod_count(self, value: int) -> None:
        self._engine.nod_count = int(value)

    @property
    def nod_decay(self) -> float:
        return self._engine.nod_decay

    @nod_decay.setter
    def nod_decay(self, value: float) -> None:
        self._engine.nod_decay = float(value)

    @property
    def nod_first_down_scale(self) -> float:
        return self._engine.nod_first_down_scale

    @nod_first_down_scale.setter
    def nod_first_down_scale(self, value: float) -> None:
        self._engine.nod_first_down_scale = float(value)

    @property
    def neck_rest_pitch_deg(self) -> float:
        return self._engine.neck_rest_pitch_deg

    @neck_rest_pitch_deg.setter
    def neck_rest_pitch_deg(self, value: float) -> None:
        self._engine.neck_rest_pitch_deg = float(value)

    @property
    def shake_amp_deg(self) -> float:
        return self._engine.shake_amp_deg

    @shake_amp_deg.setter
    def shake_amp_deg(self, value: float) -> None:
        self._engine.shake_amp_deg = float(value)

    @property
    def shake_period(self) -> float:
        return self._engine.shake_period

    @shake_period.setter
    def shake_period(self, value: float) -> None:
        self._engine.shake_period = float(value)

    @property
    def shake_swings(self) -> int:
        return self._engine.shake_swings

    @shake_swings.setter
    def shake_swings(self, value: int) -> None:
        self._engine.shake_swings = int(value)

    @property
    def shake_decay(self) -> float:
        return self._engine.shake_decay

    @shake_decay.setter
    def shake_decay(self, value: float) -> None:
        self._engine.shake_decay = float(value)

    # Two-handed wave (wave_both) posture knobs — how far the body leans back and
    # pitches nose-up before both front legs lift. Tune for tip margin on HW.
    @property
    def wave_both_lean(self) -> float:
        return self._engine.wave_both_lean

    @wave_both_lean.setter
    def wave_both_lean(self, value: float) -> None:
        self._engine.wave_both_lean = float(value)

    @property
    def wave_both_noseup(self) -> float:
        return self._engine.wave_both_noseup

    @wave_both_noseup.setter
    def wave_both_noseup(self, value: float) -> None:
        self._engine.wave_both_noseup = float(value)

    @property
    def wave_both_yaw(self) -> float:
        return self._engine.wave_both_yaw

    @wave_both_yaw.setter
    def wave_both_yaw(self, value: float) -> None:
        self._engine.wave_both_yaw = float(value)

    @property
    def wave_both_mid_forward(self) -> float:
        return self._engine.wave_both_mid_forward

    @wave_both_mid_forward.setter
    def wave_both_mid_forward(self, value: float) -> None:
        self._engine.wave_both_mid_forward = float(value)

    @property
    def wave_both_size(self) -> float:
        return self._engine.wave_both_size

    @wave_both_size.setter
    def wave_both_size(self, value: float) -> None:
        self._engine.wave_both_size = float(value)

    @property
    def wave_both_intro_speed(self) -> float:
        return self._engine.wave_both_intro_speed

    @wave_both_intro_speed.setter
    def wave_both_intro_speed(self, value: float) -> None:
        self._engine.wave_both_intro_speed = float(value)

    @property
    def wave_both_phase(self) -> float:
        return self._engine.wave_both_phase

    @wave_both_phase.setter
    def wave_both_phase(self, value: float) -> None:
        self._engine.wave_both_phase = float(value)

    @property
    def wave_both_speed(self) -> float:
        return self._engine.wave_both_speed

    @wave_both_speed.setter
    def wave_both_speed(self, value: float) -> None:
        self._engine.wave_both_speed = float(value)

    @property
    def wave_both_decay(self) -> float:
        return self._engine.wave_both_decay

    @wave_both_decay.setter
    def wave_both_decay(self, value: float) -> None:
        self._engine.wave_both_decay = float(value)

    # Clap knobs — hand open/close in separation mm, tempo, count, lift. The
    # closest approach (clap_gap) is floored inside the engine so the feet can
    # never be commanded into each other; the beg stance itself is shaped by
    # the shared wave_both_* posture knobs.
    @property
    def clap_count(self) -> int:
        return self._engine.clap_count

    @clap_count.setter
    def clap_count(self, value: int) -> None:
        self._engine.clap_count = int(value)

    @property
    def clap_period(self) -> float:
        return self._engine.clap_period

    @clap_period.setter
    def clap_period(self, value: float) -> None:
        self._engine.clap_period = float(value)

    @property
    def clap_gap(self) -> float:
        return self._engine.clap_gap

    @clap_gap.setter
    def clap_gap(self, value: float) -> None:
        self._engine.clap_gap = float(value)

    @property
    def clap_open(self) -> float:
        return self._engine.clap_open

    @clap_open.setter
    def clap_open(self, value: float) -> None:
        self._engine.clap_open = float(value)

    @property
    def clap_height(self) -> float:
        return self._engine.clap_height

    @clap_height.setter
    def clap_height(self, value: float) -> None:
        self._engine.clap_height = float(value)

    @property
    def clap_dwell(self) -> float:
        return self._engine.clap_dwell

    @clap_dwell.setter
    def clap_dwell(self, value: float) -> None:
        self._engine.clap_dwell = float(value)

    @property
    def clap_decay(self) -> float:
        return self._engine.clap_decay

    @clap_decay.setter
    def clap_decay(self, value: float) -> None:
        self._engine.clap_decay = float(value)

    @property
    def clap_intro_speed(self) -> float:
        return self._engine.clap_intro_speed

    @clap_intro_speed.setter
    def clap_intro_speed(self, value: float) -> None:
        self._engine.clap_intro_speed = float(value)

    # Dance tuning knobs (all runtime-settable). Defaults are the official
    # hardware-tuned feel baked into the engine (_DANCE_* ClassVars); documented
    # in doc/reference/api-reference.md › "Dance Tuning". ``perform_dance`` adds a lingering
    # hold and slow return; these controls shape the sway itself.
    @property
    def dance_speed(self) -> float:
        return self._engine.dance_speed

    @dance_speed.setter
    def dance_speed(self, value: float) -> None:
        self._engine.dance_speed = float(value)

    @property
    def dance_roll_deg(self) -> float:
        return self._engine.dance_roll_deg

    @dance_roll_deg.setter
    def dance_roll_deg(self, value: float) -> None:
        self._engine.dance_roll_deg = float(value)

    @property
    def dance_pivot_h(self) -> float:
        return self._engine.dance_pivot_h

    @dance_pivot_h.setter
    def dance_pivot_h(self, value: float) -> None:
        self._engine.dance_pivot_h = float(value)

    @property
    def dance_dwell(self) -> float:
        return self._engine.dance_dwell

    @dance_dwell.setter
    def dance_dwell(self, value: float) -> None:
        # Clamp to [0, 0.99): _apply_dance divides by (1 - dwell); 1.0+ would be a
        # ZeroDivisionError / flipped sway. The engine clamps too (defence in depth).
        self._engine.dance_dwell = max(0.0, min(0.99, float(value)))

    @property
    def dance_level_head(self) -> bool:
        return self._engine.dance_level_head

    @dance_level_head.setter
    def dance_level_head(self, value: bool) -> None:
        self._engine.dance_level_head = bool(value)

    def set_p_gain(self, value: int | None) -> None:
        """Set Position_P_Gain on all motors (``None`` restores defaults).

        No-op when running compute-only (no driver) or the driver does not
        support it. Raising the gain tightens tracking of the quick wave strokes.
        """
        if self._driver is not None and hasattr(self._driver, "set_position_p_gain"):
            self._driver.set_position_p_gain(value)

    def set_iir(self, enabled: bool, alpha: float | None = None) -> None:
        """Enable/disable the command-side IIR low-pass (smooths goal stream).

        No-op when running compute-only (no driver) or unsupported. *alpha* in
        (0, 1]: smaller = heavier smoothing.
        """
        if self._driver is not None and hasattr(self._driver, "set_iir"):
            self._driver.set_iir(enabled, alpha)

    @property
    def p_gain(self) -> int | None:
        """Current Position_P_Gain (None if no driver / unknown)."""
        d = self._driver
        return getattr(d, "position_p_gain", None) if d is not None else None

    def read_p_gain(self) -> dict[str, int] | None:
        """Read live Position_P_Gain back from the servos (None if no driver)."""
        d = self._driver
        if d is None or not hasattr(d, "read_position_p_gain"):
            return None
        return d.read_position_p_gain()

    @property
    def iir(self) -> tuple[bool, float] | None:
        """Current IIR (enabled, alpha), or None if no driver / unsupported."""
        d = self._driver
        if d is None or not (hasattr(d, "iir_enabled") and hasattr(d, "iir_alpha")):
            return None
        return (d.iir_enabled, d.iir_alpha)

    # ================================================================
    # NECK
    # ================================================================

    def look(
        self,
        pitch: float | NeckPitchDegrees | NeckPitchNormalized = 0.0,
        yaw: float | NeckYawDegrees | NeckYawNormalized = 0.0,
    ) -> None:
        """Set neck target (smoothly interpolated each step).

        Each axis accepts three forms:

        - a plain ``float``/``int`` — treated as normalized (the historical
          [-1, 1] contract), kept for backward compatibility. Out-of-range
          values are clamped by the engine, same as always.
        - :class:`NeckPitchNormalized` / :class:`NeckYawNormalized` — a
          fraction [-1, 1] of the neck's mechanical travel on that axis,
          passed straight through. Self-validates at construction (raises
          :class:`ValueError` outside [-1, 1]).
        - :class:`NeckPitchDegrees` / :class:`NeckYawDegrees` — a real neck
          angle for that axis, converted to normalized by dividing by the
          axis's actual mechanical travel
          (:attr:`~palmimo_sdk.engine.MotionEngine.NECK_PITCH_TRAVEL_DEG` /
          :attr:`~palmimo_sdk.engine.MotionEngine.NECK_YAW_TRAVEL_DEG`, ~26.4
          deg today on both). Self-validates at construction (raises
          :class:`ValueError` past that travel) — no clamping needed here,
          since a value object can only exist in-range.

        Passing the wrong axis's value object (e.g. a :class:`NeckYawDegrees`
        as *pitch*) raises :class:`TypeError` rather than being silently
        accepted.

        The conversion happens entirely in this facade method; the engine's
        :meth:`~palmimo_sdk.engine.MotionEngine.set_neck` still only ever
        sees a normalized float, so its pure-computation contract is
        unchanged.

        Args:
            pitch (float | NeckPitchDegrees | NeckPitchNormalized): Vertical look target. Positive tips the chin down.
            yaw (float | NeckYawDegrees | NeckYawNormalized): Horizontal look target. The sign-to-direction mapping is
                unspecified.
        """
        self._engine.set_neck(pitch=self._pitch_to_normalized(pitch), yaw=self._yaw_to_normalized(yaw))

    def _pitch_to_normalized(self, value: float | NeckPitchDegrees | NeckPitchNormalized) -> float:
        """Convert a look() pitch argument to the engine's normalized [-1, 1] float."""
        if isinstance(value, NeckPitchDegrees):
            return value.value / self._engine.NECK_PITCH_TRAVEL_DEG
        if isinstance(value, NeckPitchNormalized):
            return value.value
        if isinstance(value, NeckYawDegrees | NeckYawNormalized):
            raise TypeError(
                f"look(pitch=...) received {type(value).__name__}, a YAW value object; "
                "pass NeckPitchDegrees/NeckPitchNormalized (or a plain float) for pitch."
            )
        return float(value)

    def _yaw_to_normalized(self, value: float | NeckYawDegrees | NeckYawNormalized) -> float:
        """Convert a look() yaw argument to the engine's normalized [-1, 1] float."""
        if isinstance(value, NeckYawDegrees):
            return value.value / self._engine.NECK_YAW_TRAVEL_DEG
        if isinstance(value, NeckYawNormalized):
            return value.value
        if isinstance(value, NeckPitchDegrees | NeckPitchNormalized):
            raise TypeError(
                f"look(yaw=...) received {type(value).__name__}, a PITCH value object; "
                "pass NeckYawDegrees/NeckYawNormalized (or a plain float) for yaw."
            )
        return float(value)

    def look_center(self) -> None:
        """Return neck to center position."""
        self._engine.set_neck(0.0, 0.0)

    # ================================================================
    # FACE DISPLAY
    # ================================================================

    def set_expression(self, name: str, hold_ms: int = 0) -> str | None:
        """Show a face expression on the attached display.

        *name* is case-insensitive; firmware aliases are resolved on-device.
        ``hold_ms > 0`` auto-returns the face to IDLE after that many ms;
        ``0`` (default) holds until the next expression. Returns the firmware
        reply line, or ``None`` when no display is attached (a no-op, mirroring
        :meth:`set_p_gain` with no driver).
        """
        if self._display is not None:
            return self._display.set_expression(name, hold_ms)
        return None

    # ================================================================
    # VOICE
    # ================================================================

    def say(self, text: str, lang: str | None = None) -> SpeechHandle | None:
        """Speak *text* through the attached speaker (non-blocking).

        *lang* (``"en"`` / ``"ja"``) overrides the speaker's default voice.
        Returns the :class:`SpeechHandle` doing the speaking, so callers can
        ``join`` it and read its ``error`` to tell speech from silence, or
        ``None`` when no speaker is attached (a no-op, mirroring
        :meth:`set_expression` with no display).
        """
        if self._speaker is not None:
            return self._speaker.say(text, lang)
        return None

    def stop_speech(self) -> None:
        """Stop any in-flight speech immediately (barge-in); idempotent, and a
        no-op when no speaker is attached (mirroring :meth:`say` with no speaker).
        """
        if self._speaker is not None:
            self._speaker.stop()

    # ================================================================
    # STEP / TICK
    # ================================================================

    def step(self) -> dict[str, int]:
        """Advance one control cycle and return servo positions.

        If a driver is connected, the positions are streamed to it. If an
        ``on_step`` callback was provided, it is called afterwards.

        Returns:
            dict[str, int]: Motor name -> Dynamixel tick value.
        """
        pos = self._engine.step()
        if self._driver is not None and self._driver.is_connected:
            self._sync_gesture_tuning()
            self._sync_neck_gesture_tuning()
            self._driver.write_positions(pos)
        if self._on_step is not None:
            self._on_step(pos)
        return pos

    def _sync_gesture_tuning(self) -> None:
        """Apply/restore per-gesture servo tuning (WAVE / WAVE_BOTH / CLAP / STRETCH).

        One release-then-apply path for every gesture that tweaks servo RAM
        (Profile_Velocity / Position_P_Gain / IIR): on any change of the
        wanted state — gesture entered, left, waving leg set switched, or a
        DIRECT gesture-to-gesture switch — the previous gesture's tuning is
        released first, then the new one applied. Two separate per-gesture
        sync methods could not do this safely: whichever ran second stomped
        the other's fresh apply with its exit-restore. The release restores
        the driver's OWN configured profile velocity, not a hardcoded
        default. Extend the want-state below when a new tuned gesture lands.
        Best-effort: drivers without these RAM tweaks simply no-op — STRETCH
        only needs Profile_Velocity, so it still tunes on a PV-only driver
        while the wave family additionally requires Position_P_Gain / IIR.
        """
        driver = self._driver
        if driver is None or not hasattr(driver, "set_profile_velocity_units"):
            return
        has_gain = hasattr(driver, "set_position_p_gain")
        has_iir = hasattr(driver, "set_iir")

        motion = self._engine.motion
        # Wave family (needs Profile_Velocity + Position_P_Gain + IIR): the
        # want key carries the waving leg set and the arm IIR weight, so a
        # wave_left -> wave_right or wave -> clap switch re-applies. STRETCH
        # only tweaks leg Profile_Velocity, so it tunes even on a PV-only
        # driver.
        if motion == Motion.WAVE and has_gain and has_iir:
            want: tuple | None = ("wave", (self._engine.wave_leg,), _WAVE_ARM_IIR)
        elif motion == Motion.WAVE_BOTH and has_gain and has_iir:
            want = ("wave", self._engine._WAVE_BOTH_LEGS, _WAVE_ARM_IIR)
        elif motion == Motion.CLAP and has_gain and has_iir:
            # Lighter smoothing: the wave's IIR would low-pass the fast
            # open/close away (see _CLAP_ARM_IIR).
            want = ("wave", self._engine._WAVE_BOTH_LEGS, _CLAP_ARM_IIR)
        elif motion == Motion.STRETCH:
            want = ("stretch",)
        else:
            want = None
        if want == self._gesture_tuned:
            return  # nothing changed

        default_pv = getattr(driver, "profile_velocity", None)
        if default_pv is None:
            default_pv = _WAVE_LEG_PV

        # Release whatever the previous gesture applied.
        if self._gesture_tuned is not None:
            with contextlib.suppress(Exception):
                if self._gesture_tuned[0] == "wave":
                    if hasattr(driver, "set_iir"):
                        driver.set_iir(False)
                    driver.set_position_p_gain(None)  # restore captured defaults
                driver.set_profile_velocity_units(default_pv)

        # Apply the new gesture's tuning.
        if want is not None and want[0] == "wave":
            legs, arm_iir = want[1], want[2]
            arm = [f"leg_{leg}_{axis}" for leg in legs for axis in ("yaw", "pitch1", "pitch2")]
            with contextlib.suppress(Exception):
                driver.set_profile_velocity_units(default_pv)  # legs smooth
                driver.set_profile_velocity_units(_WAVE_ARM_PV, motors=arm)  # arm crisp
                driver.set_position_p_gain(_WAVE_GAIN)
                if hasattr(driver, "set_iir"):
                    driver.set_iir(True, arm_iir, motors=arm)
        elif want is not None:
            with contextlib.suppress(Exception):
                driver.set_profile_velocity_units(_STRETCH_LEG_PV)

        self._gesture_tuned = want

    def _sync_neck_gesture_tuning(self) -> None:
        """Apply/restore the neck servo tuning around a nod / head-shake.

        Mirrors :meth:`_sync_gesture_tuning` for the neck gestures: on entry the
        two driven neck axes get Profile_Velocity 0 so the quick strokes are
        not smeared by the default time-based profile; on exit the default
        profile is restored. Best-effort — drivers without the RAM tweak
        (compute-only, or a non-Dynamixel backend) simply no-op.
        """
        driver = self._driver
        if driver is None or not hasattr(driver, "set_profile_velocity_units"):
            return
        active = self._engine.motion in NECK_GESTURES
        if active == self._neck_gesture_tuned:
            return  # nothing changed
        # Restore to the driver's OWN default profile, not a baked constant —
        # the driver may have been constructed with a non-default one.
        restore_pv = getattr(driver, "profile_velocity", _NECK_GESTURE_RESTORE_PV_FALLBACK)
        with contextlib.suppress(Exception):
            driver.set_profile_velocity_units(
                _NECK_GESTURE_PV if active else restore_pv,
                motors=list(_NECK_GESTURE_MOTORS),
            )
            # State advances only after the write succeeds (unlike the wave
            # tuning) so a failed write is retried on the next cycle.
            self._neck_gesture_tuned = active

    def step_n(self, n: int) -> list[dict[str, int]]:
        """Advance *n* steps, returning all positions."""
        return [self.step() for _ in range(n)]

    @staticmethod
    def _dance_sway_frames(sways: int, phase_inc: float) -> int:
        """Frames to sway *sways* times, ENDING ON AN EXTREME (not at center).

        The sway is ``sin(2π·phase)`` starting at phase 0 (center), so the
        extremes fall at phase ``0.25 + k·0.5``. Ending *sways* swings there is
        phase ``0.5·sways − 0.25`` (1 → 0.25, the first extreme reached from
        center; each later swing adds a half-cycle side-to-side). A naive
        ``0.5·sways`` would instead stop at a CENTER crossing, so the end-hold
        would freeze mid-sway rather than at the held extreme. Returns 0 when
        there is nothing to sway.
        """
        if sways <= 0 or phase_inc <= 0:
            return 0
        return max(1, round((sways * 0.5 - 0.25) / phase_inc))

    @staticmethod
    def _duration_to_steps(seconds: float, fps: int) -> int:
        """Convert a wall-clock duration to a control-cycle count.

        The single seconds→steps rule shared by :meth:`run` and :meth:`play`,
        so the two paths can never disagree on how many cycles a duration is.
        """
        return round(seconds * fps)

    def _pace(
        self,
        frames: Iterable[dict[str, int]],
        fps: int,
        cancel_snapshot: int,
        callback: Callable[[dict[str, int]], None] | None = None,
    ) -> dict[str, int]:
        """Drive *frames* in real time at *fps*, returning the last frame.

        Schedules each frame against an absolute deadline (``start + i*dt``)
        rather than sleeping a flat ``dt`` per frame, so per-frame compute time
        doesn't accumulate into drift over a long run. The single pacing rule
        shared by :meth:`run`, :meth:`perform_dance`, and :meth:`play_realtime`.

        Args:
            cancel_snapshot (int): The value of :attr:`_cancel_count` the calling public method
                captured at its own entry (see :meth:`cancel`'s docstring for
                why this is a snapshot-and-compare counter rather than a
                clear()-able ``Event``). Every frame boundary compares the
                LIVE counter against this snapshot rather than clearing
                anything here — clearing inside ``_pace`` itself would
                reopen exactly the race the counter design exists to close.

        Raises:
            MotionCancelled: If :meth:`cancel` runs (incrementing :attr:`_cancel_count` past
                *cancel_snapshot*) while frames are being paced. Checked once
                per frame — after that frame's callback, before sleeping to
                the next frame's deadline — so a cancellation is caught
                within roughly one frame period (``1/fps`` s) instead of only
                once the whole call finishes.
        """
        dt = 1.0 / fps
        start = time.perf_counter()
        last: dict[str, int] = {}
        for i, pos in enumerate(frames):
            last = pos
            if callback is not None:
                callback(pos)
            self._check_cancelled(cancel_snapshot)
            remaining = start + (i + 1) * dt - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
        return last

    def run(
        self,
        *,
        seconds: float | None = None,
        steps: int | None = None,
    ) -> dict[str, int]:
        """Run the current motion for a fixed duration, real-time paced.

        Blocking. Advances the engine at the facade's control rate
        (:attr:`fps`) and sleeps so that *steps* cycles take ``steps / fps``
        seconds of wall clock. Each cycle streams to the driver and fires
        ``on_step`` exactly like :meth:`step`. Set the motion first
        (``robot.forward()``), then ``robot.run(seconds=1.5)``.

        For unpaced loops (simulation, tests, custom timing) use :meth:`step`
        / :meth:`step_n` instead — they compute as fast as possible.

        Cancellable from another thread: see :meth:`cancel`. Takes its entry
        snapshot via :meth:`_take_cancel_snapshot` the instant it starts
        (before any validation) — a ``cancel()`` delivered while idle (no
        ``run()`` in flight, and no armed scope pending) does not carry over
        and cancel THIS call — then checks it once per frame inside
        :meth:`_pace`, raising :class:`MotionCancelled` there if ``cancel()``
        ran meanwhile. A caller that armed a cancel scope first (see
        :meth:`_arm_cancel_scope`) has that armed value consumed here
        instead, which additionally catches a ``cancel()`` delivered after
        arming but before this call was even entered.

        Args:
            seconds (float, optional): Wall-clock duration. Converted to ``round(seconds * fps)`` cycles.
            steps (int, optional): Exact number of control cycles.

        Exactly one of *seconds* / *steps* must be given, and it must be
        non-negative.

        Returns:
            dict[str, int]: The final servo positions (current pose if zero cycles ran).

        Raises:
            ValueError: If neither or both of *seconds* / *steps* are given, or the value
                is negative.
            MotionCancelled: If :meth:`cancel` is called while this run() is in flight.
        """
        snapshot = self._take_cancel_snapshot()
        if seconds is not None and steps is not None:
            raise ValueError("Pass exactly one of seconds= or steps=.")
        if seconds is not None:
            if seconds < 0:
                raise ValueError("seconds must be >= 0.")
            n = self._duration_to_steps(seconds, self._fps)
        elif steps is not None:
            if steps < 0:
                raise ValueError("steps must be >= 0.")
            n = steps
        else:
            raise ValueError("Pass exactly one of seconds= or steps=.")

        if n == 0:
            return self.positions
        return self._pace((self.step() for _ in range(n)), self._fps, snapshot)

    def perform_dance(
        self,
        *,
        sways: int = _DANCE_SWAYS,
        end_hold: float = _DANCE_END_HOLD_S,
        settle: float = _DANCE_SETTLE_S,
    ) -> dict[str, int]:
        """Play one complete, self-terminating dance performance (blocking).

        The finished gesture used by the demo and dance voice command — the
        analogue of the wave's one-shot greeting. The continuous :meth:`dance`
        motion is unchanged; call that and :meth:`step` in a loop for an
        open-ended sway.

        Parameters, defaults, and the finishing flourish (the soft end-hold + the
        glide-home, and why they avoid overshoot / a hard stop) are documented in
        doc/reference/api-reference.md › "Dance Tuning" — the single source of truth — and
        the ``_DANCE_*`` constants hold the in-code Why. Kept out of this
        docstring so the same text isn't maintained twice.

        Cancellable end-to-end, like :meth:`run`: :meth:`cancel` aborts
        whichever section is currently running (the sway, the end-hold, or
        the glide home) within roughly one frame/poll period, raising
        :class:`MotionCancelled`. Whichever section it happened in, ``stop()``
        (IDLE) runs before the exception propagates, so the facade never
        stays in the DANCE motion after being cancelled.
        """
        snapshot = self._take_cancel_snapshot()
        try:
            self.stop()
            self.dance()  # Motion.DANCE setter resets gait phase to 0 (sway from center)
            n = self._dance_sway_frames(sways, self._engine.dance_speed * 0.8)
            if n:
                self._pace((self.step() for _ in range(n)), self._fps, snapshot)
            self._dance_end_hold(end_hold, snapshot)
            # Glide home, then drop to IDLE so the next motion starts from neutral.
            # The timed glide needs a connected driver (it reads present positions);
            # compute-only (sim/tests) eases via the convergence path instead.
            if settle > 0 and self.is_connected:
                self.return_to_neutral(duration=settle, _cancel_snapshot=snapshot)
            else:
                self.return_to_neutral(_cancel_snapshot=snapshot)
        except MotionCancelled:
            # Guarantee IDLE before the cancellation unwinds further, so a
            # caller catching MotionCancelled never observes the facade
            # still parked in Motion.DANCE. Then glide the legs to a stance
            # pose (best-effort; a second cancel() abandons it) -- hardware
            # verification showed an interrupted routine otherwise leaves
            # legs frozen mid-stride, bent and loaded, until the caller's
            # next move.
            self.stop()
            with contextlib.suppress(MotionCancelled):
                self.run(seconds=_CANCEL_SETTLE_S)
            raise
        self.stop()
        return self.positions

    def _dance_end_hold(self, seconds: float, cancel_snapshot: int) -> None:
        """Soft-freeze the final dance pose to create a lingering finish.

        Re-commands ONLY the legs at their present pose with a lowered
        Position_P_Gain, so they hold where the sway actually left them instead
        of creeping out to the last full-amplitude goal they were lagging toward
        (which would overshoot past the sway just shown). The neck is left out:
        :meth:`write_positions` backfills it to neutral so the head stays up, and
        its gain is untouched so it can't droop. Best-effort — with no driver, or
        one without the RAM gain control, it simply pauses for *seconds*.

        The pause (with or without a driver) is cancellable, via
        :meth:`_sleep_cancellable` against *cancel_snapshot* — a
        :class:`MotionCancelled` raised here still restores the driver's
        default gain first (in a ``finally``), so a cancellation mid-hold
        doesn't leave the legs at the lowered hold gain.
        """
        if seconds <= 0:
            return
        driver = self._driver
        can_freeze = (
            driver is not None
            and driver.is_connected
            and hasattr(driver, "read_positions")
            and hasattr(driver, "set_position_p_gain")
        )
        if not can_freeze:
            self._sleep_cancellable(seconds, cancel_snapshot)
            return
        assert driver is not None
        legs = [n for n in self._engine.get_positions() if n not in _NECK_MOTORS]
        with contextlib.suppress(Exception):
            present = driver.read_positions()
            if present:
                leg_present = {n: p for n, p in present.items() if n in legs}
                driver.set_position_p_gain(_DANCE_HOLD_GAIN, legs)
                driver.write_positions(leg_present)  # neck backfilled to NEUTRAL (head up)
        try:
            self._sleep_cancellable(seconds, cancel_snapshot)
        finally:
            with contextlib.suppress(Exception):
                driver.set_position_p_gain(None)  # restore default gain before the glide home

    @property
    def positions(self) -> dict[str, int]:
        """Current servo positions without advancing phase."""
        return self._engine.get_positions()

    @property
    def motion(self) -> str:
        """Current motion name as a lowercase string."""
        return self._engine.motion.name.lower()

    def reset(self) -> None:
        """Immediately reset all servos to neutral (no smooth transition)."""
        self._engine.reset()

    # ================================================================
    # CHOREOGRAPHY
    # ================================================================

    def _coerce_step(self, entry: RoutineStep | tuple[str, float]) -> RoutineStep:
        """Normalize a routine entry to a :class:`RoutineStep`.

        Accepts a :class:`RoutineStep` as-is, or a legacy ``(name, seconds)``
        tuple. The legacy special name ``"look_around"`` maps to a neck sweep
        (``neck_sweep=True``); any other name is resolved through
        ``_MOTION_MAP`` (raising :class:`ValueError` if unknown).
        """
        if isinstance(entry, RoutineStep):
            return entry
        name, seconds = entry
        if name == "look_around":
            return RoutineStep(seconds=seconds, neck_sweep=True)
        key = name.lower().strip()
        if key not in self._MOTION_MAP:
            available = ", ".join(sorted(self._MOTION_MAP.keys()))
            raise ValueError(f"Unknown motion {name!r}. Choose from: {available}")
        # Carry the L/R side so play() picks the same leg set_motion() would.
        wave_leg = self._WAVE_LEG_FOR.get(key)
        return RoutineStep(motion=self._MOTION_MAP[key], seconds=seconds, wave_leg=wave_leg)

    def play(
        self,
        routine: Sequence[RoutineStep | tuple[str, float]],
        fps: int | None = None,
    ) -> Generator[dict[str, int], None, None]:
        """Play a choreography routine, yielding positions for each frame.

        Each entry is a :class:`RoutineStep` (a leg :class:`Motion` plus an
        optional neck sweep) or a legacy ``(name, seconds)`` tuple. A cue with
        ``neck_sweep=True`` sinusoidally pans/tilts the neck while the legs
        keep running its *motion*, then recenters the neck when the cue ends.

        Args:
            routine (sequence of RoutineStep or (str, float)): The cues to play, in order.
            fps (int, optional): Frames per second. Defaults to the facade's :attr:`fps`.

        Yields:
            dict[str, int]: Servo positions for each frame.
        """
        fps = fps if fps is not None else self._fps
        # A per-call fps override also retimes the engine's second-based
        # posture one-shots, so e.g. a 30 fps routine still plays a bow at its
        # wall-clock length. The rate is re-asserted before EVERY frame (not
        # saved and restored around the routine): interleaved play()
        # generators then each step at their own rate, and the ``finally``
        # lands on the facade's rate instead of a stale value captured from
        # another, possibly abandoned, generator.
        try:
            for entry in routine:
                cue = self._coerce_step(entry)
                steps = self._duration_to_steps(cue.seconds, fps)
                if cue.wave_leg is not None:
                    self._engine.wave_leg = cue.wave_leg
                self._engine.motion = cue.motion

                if cue.neck_sweep:
                    for i in range(steps):
                        t = i / fps
                        self.look(
                            pitch=0.5 * math.sin(2 * math.pi * 0.4 * t),
                            yaw=0.8 * math.sin(2 * math.pi * 0.25 * t),
                        )
                        self._engine.dt = 1.0 / fps
                        yield self.step()
                    self.look_center()
                else:
                    for _ in range(steps):
                        self._engine.dt = 1.0 / fps
                        yield self.step()
        finally:
            self._engine.dt = 1.0 / self._fps

    def play_realtime(
        self,
        routine: Sequence[RoutineStep | tuple[str, float]],
        fps: int | None = None,
        callback: Callable[[dict[str, int]], None] | None = None,
    ) -> None:
        """Play a choreography in real time (blocking).

        Like :meth:`play` but paces itself with ``time.sleep`` at *fps*.

        Args:
            routine (sequence of RoutineStep or (str, float)): The cues to play, in order.
            fps (int, optional): Frames per second. Defaults to the facade's :attr:`fps`.
            callback (callable, optional): Extra per-frame callback. The ``on_step`` callback from init still
                fires once per frame via :meth:`step`; this is *in addition* to it
                (not a fallback), so passing ``on_step`` here would double-fire it.

        Cancellable from another thread, like :meth:`run`: see :meth:`cancel`.
        Snapshots :attr:`_cancel_count` at its own entry, before anything
        else runs.
        """
        snapshot = self._take_cancel_snapshot()
        fps = fps if fps is not None else self._fps
        self._pace(self.play(routine, fps=fps), fps, snapshot, callback=callback)

    # ================================================================
    # ENGINE ACCESS (advanced)
    # ================================================================

    @property
    def engine(self) -> MotionEngine:
        """Direct access to the underlying :class:`MotionEngine`.

        For advanced use cases like custom gaits or IK experiments.
        """
        return self._engine
