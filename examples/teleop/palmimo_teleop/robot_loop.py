"""Background thread that drives `Palmimo` from a `session.PilotSession` at a fixed
control rate.

Owns the only thread allowed to call into `Palmimo` after `connect()` --
`server.py`'s WebSocket handlers only ever write to the session, never touch
the robot directly, so servo I/O never races across threads.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from palmimo_sdk import Palmimo

from .control import DEFAULT_SPEED, GAIT_MOTIONS, LEG_MOTORS, MAX_GAIT_SPEED, resolve_motion
from .session import PilotSession


_LOG = logging.getLogger(__name__)

#: Sentinel for "no tick has applied a motion yet", distinct from the `None`
#: `resolve_motion` returns for "stop" -- see `RobotLoop._applied_motion`.
_UNSET = object()

#: How long `shutdown()` keeps stepping a stopped robot before returning, so a
#: settling gait (mid-stride when the loop was asked to stop) eases to
#: neutral at the control rate instead of being cut off mid-step. Mirrors the
#: gamepad example's `main.SETTLE_SECONDS`.
SETTLE_SECONDS: float = 1.0

#: Minimum gap between consecutive "tick failed" log lines once a failure is
#: already being logged, so a persistently faulting servo bus logs at roughly
#: this rate instead of once per control cycle (up to 60/s).
FAILURE_LOG_INTERVAL_S: float = 5.0

#: First wait before retrying a failed leg Profile_Velocity write, doubled
#: after each further consecutive failure up to `LEG_PROFILE_RETRY_MAX_S`.
#: Kept apart from `FAILURE_LOG_INTERVAL_S` (which only throttles the log):
#: a flaky bus must not cost every 60 fps cycle up to 18 write timeouts, yet a
#: restore must still get several tries inside the one-second settle.
LEG_PROFILE_RETRY_BASE_S: float = 0.1
LEG_PROFILE_RETRY_MAX_S: float = 1.0

#: Upper bound on leg Profile_Velocity restore attempts during `_settle()`,
#: spaced `LEG_PROFILE_RETRY_BASE_S` apart. If the legs are still not back at
#: the driver default after these, settle finishes anyway and the caller
#: disconnects (torque off) -- a bus that cannot take the write cannot be
#: waited on forever at shutdown, and the next start rewrites every leg.
LEG_PROFILE_SETTLE_ATTEMPTS: int = 5

#: Gestures whose servo tuning `Palmimo` applies (and later releases) itself
#: inside `step()`, provided the driver has the Position_P_Gain / IIR knobs
#: that tuning needs -- see `Palmimo._sync_gesture_tuning`. While one of these
#: plays the loop never writes the leg profile, so it cannot overwrite the
#: wave's crisp arm profile. Any other gesture (dance) gets no tuning from
#: `Palmimo`, so the loop itself restores the legs to the driver default.
SDK_TUNED_GESTURES: frozenset[str] = frozenset({"wave"})

#: Marker in `RobotLoop._leg_pv_known` for a leg whose last write failed:
#: its Profile_Velocity is unknown and possibly still shortened.
_PV_UNKNOWN = -1

#: Sign applied to the pilot's raw stick value before `Palmimo.look()`.
#: `Palmimo.look`'s sign-to-direction mapping is itself unspecified (see its
#: docstring); on a real neck both axes track the stick as-is.
NECK_PITCH_SIGN: float = 1.0
NECK_YAW_SIGN: float = 1.0


class RobotLoop:
    """Drives *robot* from *session* at `fps` control cycles per second on a daemon thread.

    Each cycle: read `session.effective_input()`, resolve it to a motion name
    (`control.resolve_motion`), apply it (`robot.set_motion` / `robot.stop`)
    and the neck target (`robot.look`), then `robot.step()`. `tick()` is the
    single-cycle body, exposed separately from `start()`/`_run()` so tests can
    drive it deterministically against a fake robot and session without
    threads or real time.

    Applying the resolved motion is edge-triggered: `robot.set_motion`/
    `robot.stop` is only called when the resolved name differs from the one
    applied last tick. `MotionEngine` resets a WAVE/DANCE motion's phase to 0
    every time it is (re)selected (`engine.py`'s `motion` setter), so calling
    `set_motion("wave")` every tick -- as a naive per-tick apply would -- would
    restart wave from its first frame forever instead of letting it play.
    Walking gaits do not reset on reselection, so this changes nothing for them.

    The pilot's speed multiplier is applied the same edge-triggered way,
    before the motion, to walking and rotating only: `robot.engine.gait_speed`
    (`Palmimo` takes gait speed only as a constructor argument) becomes the
    startup gait speed times the multiplier, capped at
    `control.MAX_GAIT_SPEED`. Wave and dance always play at their own speed.

    Speeding up the commands alone is not enough for the gait: the driver's
    time-based Profile_Velocity (each goal reached over a fixed number of
    milliseconds) acts as a low-pass filter on the leg servos, so a faster
    gait comes out with a smaller stride and barely more ground speed. While
    a gait runs, the leg servos' Profile_Velocity is therefore divided by the
    same factor as the gait speed, which compresses the servo response in
    time along with the commands and keeps the 1x stride. It is written
    after `robot.step()`, because `Palmimo` restores the driver default
    inside `step()` when a gesture ends. Switching to a gesture puts the legs
    back to the driver default *before* `step()` instead -- `Palmimo` retunes
    the servos for a wave inside that `step()` but leaves them as they are
    for a dance -- and nothing is written while a wave plays.

    These writes are best-effort and never fail the cycle. The loop tracks
    each leg's last confirmed value, so a write that fails partway leaves the
    legs it may not have reached marked unknown ("dirty") rather than assumed
    written. Dirty legs are retried -- at once when the motion or speed
    changes, otherwise on a back-off from `LEG_PROFILE_RETRY_BASE_S` to
    `LEG_PROFILE_RETRY_MAX_S` -- while standing, walking or dancing, and up to
    `LEG_PROFILE_SETTLE_ATTEMPTS` times during shutdown's settle. During a
    wave the loop hands the legs to `Palmimo` and assumes nothing: `Palmimo`'s
    own wave writes can fail too (it suppresses their errors), so every leg is
    rewritten once the wave ends.
    """

    def __init__(
        self,
        robot: Palmimo,
        session: PilotSession,
        *,
        fps: int = 60,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        settle_timeout: float = SETTLE_SECONDS + 1.0,
    ) -> None:
        self.robot = robot
        self.session = session
        self.fps = fps
        self._now = now
        self._sleep = sleep
        self._settle_timeout = settle_timeout
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._fps_lock = threading.Lock()
        self._loop_fps = 0.0
        self._window_start = 0.0
        self._window_frames = 0
        self._ok_lock = threading.Lock()
        self._robot_ok = True
        self._last_failure_log_at: float | None = None
        #: The motion name applied on the previous tick, for the
        #: edge-triggered `set_motion`/`stop` call -- see the class
        #: docstring. `_UNSET` (not `None`) before the first tick, so that
        #: tick's `robot.stop()` is issued even when the resolved motion is
        #: also `None`.
        self._applied_motion: str | object | None = _UNSET
        #: The gait speed the robot was started with -- what 1x means. Read
        #: once, so each multiplier scales `--gait-speed` instead of
        #: compounding on a value an earlier multiplier already wrote.
        self._base_gait_speed: float = robot.engine.gait_speed
        #: The speed multiplier currently written to `robot`; starts at the
        #: default because that is what `_base_gait_speed` was read at.
        self._applied_speed = DEFAULT_SPEED
        #: Per leg servo, the Profile_Velocity this loop last confirmed
        #: writing, or `_PV_UNKNOWN` after a write that failed. A missing leg
        #: is one the loop has not written since start or since handing the
        #: legs to `Palmimo` for a wave -- never dirty, but always rewritten
        #: when the loop next wants a value on it.
        self._leg_pv_known: dict[str, int] = {}
        self._leg_pv_failures = 0
        self._leg_pv_next_try_at: float | None = None
        #: Whether this loop has ever written (or tried to write) the leg
        #: profile -- settle restores nothing on a robot it never retuned.
        self._leg_pv_touched = False
        self._last_profile_log_at: float | None = None

    @property
    def loop_fps(self) -> float:
        """The measured effective control rate over the last full one-second window.

        `0.0` until the loop has run for at least a second -- the configured
        `fps` is only ever a target; this is what it actually achieved. A
        tick that raised does not count as a frame here (see `tick()`), so a
        servo bus failing most cycles reports a correspondingly low rate
        rather than one that looks healthy.
        """
        with self._fps_lock:
            return self._loop_fps

    @property
    def robot_ok(self) -> bool:
        """Whether the most recent control cycle completed without raising."""
        with self._ok_lock:
            return self._robot_ok

    def tick(self) -> bool:
        """Run one control cycle: resolve `session`'s current input and apply it to `robot`.

        Never raises: a failing cycle (a servo bus fault, mid-run) is caught,
        recorded via `robot_ok`, and logged at a throttled rate rather than
        propagating out of the background thread and silently killing it.

        Returns:
            bool: `True` if the cycle completed, `False` if it raised.
        """
        try:
            effective = self.session.effective_input()
            speed_changed = effective.speed != self._applied_speed
            if speed_changed:
                self._apply_speed(effective.speed)
            motion = resolve_motion(effective.move, effective.rotate, effective.gesture)
            motion_changed = motion != self._applied_motion
            if motion_changed:
                if motion is None:
                    self.robot.stop()
                else:
                    self.robot.set_motion(motion)
                self._applied_motion = motion
            gait_or_idle = motion is None or motion in GAIT_MOTIONS
            if not gait_or_idle:
                # Only a change of *motion* is a gesture's first cycle; a speed
                # tap mid-wave must not re-trigger the pre-wave write.
                self._leg_profile_before_gesture(motion, entering=motion_changed)
            self.robot.look(pitch=effective.neck_pitch * NECK_PITCH_SIGN, yaw=effective.neck_yaw * NECK_YAW_SIGN)
            self.robot.step()
            if gait_or_idle:
                self._sync_leg_profile(motion, force=motion_changed or speed_changed)
        except Exception:
            self._record_tick_failure()
            return False
        with self._ok_lock:
            self._robot_ok = True
        return True

    def _apply_speed(self, multiplier: float) -> None:
        """Write the startup gait speed times *multiplier* to `robot`."""
        self.robot.engine.gait_speed = min(MAX_GAIT_SPEED, self._base_gait_speed * multiplier)
        self._applied_speed = multiplier

    def applied_multiplier(self, selected: float) -> float:
        """How much faster than 1x the gait actually runs when *selected* is chosen.

        Equal to *selected* unless `control.MAX_GAIT_SPEED` caps it -- with
        `--gait-speed 0.05`, every multiplier runs at 1x. Pure arithmetic on
        the startup gait speed, so the status broadcast can call it from the
        event loop without touching `robot`.
        """
        if self._base_gait_speed <= 0:
            return selected
        return min(MAX_GAIT_SPEED, self._base_gait_speed * selected) / self._base_gait_speed

    def _profile_driver(self) -> tuple[Any, int] | None:
        """The driver and its default Profile_Velocity, or `None` if it has no
        time-based profile knob (compute-only, or a non-Dynamixel backend)."""
        driver = self.robot.driver
        if driver is None or not hasattr(driver, "set_profile_velocity_units"):
            return None
        default = getattr(driver, "profile_velocity", None)
        if not isinstance(default, int):
            return None
        return driver, default

    def _sdk_tunes(self, driver: Any, motion: str | None) -> bool:
        """Whether `Palmimo` applies its own servo tuning to *motion* on this driver."""
        return motion in SDK_TUNED_GESTURES and hasattr(driver, "set_position_p_gain") and hasattr(driver, "set_iir")

    def _pending_legs(self, value: int) -> list[str]:
        """Legs not confirmed at *value* (never written, written otherwise, or a failed write)."""
        return [m for m in LEG_MOTORS if self._leg_pv_known.get(m) != value]

    def _dirty_legs(self, default: int) -> list[str]:
        """Legs the loop may have left off the driver default (shortened, or a failed write)."""
        return [m for m in LEG_MOTORS if m in self._leg_pv_known and self._leg_pv_known[m] != default]

    def _retry_due(self) -> bool:
        return self._leg_pv_next_try_at is None or self._now() >= self._leg_pv_next_try_at

    def _sync_leg_profile(self, motion: str | None, *, force: bool = False) -> None:
        """Write the leg Profile_Velocity a gait (*motion*) or standing still (`None`) calls for.

        A gait gets the driver default divided by how much faster than its
        startup speed the gait now runs (so a capped gait speed is matched
        too); standing still gets the driver default. Only legs not already
        confirmed at that value are written; after a failure they are retried
        on the back-off, or at once when *force* (the motion or speed changed).
        """
        found = self._profile_driver()
        if found is None:
            return
        driver, default = found
        if motion in GAIT_MOTIONS:
            ratio = self.robot.engine.gait_speed / self._base_gait_speed
            want = max(1, round(default / ratio)) if default > 0 else 0
        else:
            want = default
        pending = self._pending_legs(want)
        if pending and (force or self._retry_due()):
            self._write_leg_profile(driver, want, pending)

    def _leg_profile_before_gesture(self, motion: str | None, *, entering: bool) -> None:
        """Before a gesture's `step()`, put any leg the loop shortened back to the driver default.

        For a wave `Palmimo` tunes the servos itself: the restore is tried
        once, on the wave's first cycle only (by the next cycle `Palmimo` has
        applied the wave's arm profile, which a later write would overwrite),
        and the legs are then handed over -- forgotten, so every leg is
        rewritten after the wave instead of trusting `Palmimo`'s own writes,
        whose errors it suppresses. For a dance (no tuning from `Palmimo`)
        dirty legs are retried every cycle on the back-off until confirmed.
        """
        found = self._profile_driver()
        if found is None:
            return
        driver, default = found
        dirty = self._dirty_legs(default)
        if not self._sdk_tunes(driver, motion):
            if dirty and (entering or self._retry_due()):
                self._write_leg_profile(driver, default, dirty)
            return
        if not entering:
            return
        if dirty and not self._write_leg_profile(driver, default, dirty):
            _LOG.warning(
                "leg Profile_Velocity not restored before %s; left to Palmimo's own tuning, "
                "and every leg is rewritten when it ends",
                motion,
            )
        self._leg_pv_known.clear()

    def _write_leg_profile(self, driver: Any, value: int, motors: list[str]) -> bool:
        """Write *value* to *motors*' Profile_Velocity; `False` (logged, throttled) if it failed.

        The driver writes motor by motor, so a failure may land after some of
        *motors* already took the value: all of them are marked unknown, to be
        rewritten, and the next try is pushed back on the back-off.
        """
        self._leg_pv_touched = True
        try:
            driver.set_profile_velocity_units(value, motors=list(motors))
        except Exception:
            for motor in motors:
                self._leg_pv_known[motor] = _PV_UNKNOWN
            self._leg_pv_failures += 1
            now = self._now()
            delay = min(LEG_PROFILE_RETRY_MAX_S, LEG_PROFILE_RETRY_BASE_S * 2 ** (self._leg_pv_failures - 1))
            self._leg_pv_next_try_at = now + delay
            if self._last_profile_log_at is None or now - self._last_profile_log_at >= FAILURE_LOG_INTERVAL_S:
                _LOG.warning(
                    "leg Profile_Velocity write failed (%d in a row); retrying in %.1fs",
                    self._leg_pv_failures,
                    delay,
                    exc_info=True,
                )
                self._last_profile_log_at = now
            return False
        for motor in motors:
            self._leg_pv_known[motor] = value
        self._leg_pv_failures = 0
        self._leg_pv_next_try_at = None
        return True

    def _record_tick_failure(self) -> None:
        with self._ok_lock:
            self._robot_ok = False
        now = self._now()
        if self._last_failure_log_at is None or now - self._last_failure_log_at >= FAILURE_LOG_INTERVAL_S:
            _LOG.exception("robot loop tick failed")
            self._last_failure_log_at = now

    def _update_loop_fps(self, tick_succeeded: bool, frame_start: float) -> None:
        """Fold one cycle's outcome into the one-second `loop_fps` window.

        Split out of `_run()` (like `tick()`) so tests can drive the window
        bookkeeping deterministically against a fake clock.
        """
        if tick_succeeded:
            self._window_frames += 1
        window_elapsed = frame_start - self._window_start
        if window_elapsed >= 1.0:
            with self._fps_lock:
                self._loop_fps = self._window_frames / window_elapsed
            self._window_start = frame_start
            self._window_frames = 0

    def start(self) -> None:
        """Start the background thread. A no-op if already running.

        "Already running" includes a thread `shutdown()` failed to join
        within `settle_timeout`: that thread's handle is kept (not cleared)
        precisely so this check still sees it and refuses to start a second
        one alongside it.
        """
        if self._thread is not None and self._thread.is_alive():
            _LOG.warning("robot loop thread is still running -- refusing to start a second one")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="palmimo-teleop-robot-loop", daemon=True)
        self._thread.start()

    def shutdown(self) -> bool:
        """Stop the background thread, then settle the robot to idle before returning.

        Safe to call whether or not `start()` was ever called (a `--dry-run`
        with `session` never producing motion still wants a clean `stop()`).

        If the thread does not stop within `settle_timeout`, `_settle()` is
        skipped and an error is logged instead of running it anyway: `_run()`
        is the only thread allowed to call into `robot` once connected, and
        running `_settle()` concurrently with a still-alive `_run()` would
        break that invariant. The thread handle is kept (not cleared) in this
        case, both so `start()` can refuse to start a second thread and so
        the caller knows -- via the `False` return -- that `robot` is still
        being driven by the stuck thread and must not be touched (e.g.
        disconnected) from anywhere else.

        Returns:
            bool: `True` if the loop stopped and settled; `False` if the
                thread was still alive after `settle_timeout` and settling
                was skipped.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._settle_timeout)
            if self._thread.is_alive():
                _LOG.error(
                    "robot loop thread did not stop within %.1fs -- skipping settle "
                    "to avoid a second thread driving the robot concurrently",
                    self._settle_timeout,
                )
                return False
            self._thread = None
        self._settle()
        return True

    def _settle(self) -> None:
        """Stop the robot and step it for `SETTLE_SECONDS`, paced at `fps`.

        Run after the drive loop has already exited, so the settle steps
        never race `tick()` -- paced rather than issued back-to-back, matching
        the "gradually" contract `Palmimo.stop()` promises on real hardware.
        A raised speed multiplier, and the leg servo profile shortened with
        it, are put back to the defaults, so the robot eases to a stop at its
        own tuned speed. The leg restore is tried before `stop()` (unless a
        wave still owns the servos -- `Palmimo` releases it on the next
        `step()`) and again after settle steps `LEG_PROFILE_RETRY_BASE_S`
        apart, until every leg is confirmed at the driver default or
        `LEG_PROFILE_SETTLE_ATTEMPTS` is spent. A restore still failing then
        is logged as an error and the caller disconnects (torque off) anyway.
        """
        if self._applied_speed != DEFAULT_SPEED:
            self._apply_speed(DEFAULT_SPEED)
        found = self._profile_driver() if self._leg_pv_touched else None
        attempts = 0

        def try_restore() -> None:
            nonlocal attempts
            if found is None or attempts >= LEG_PROFILE_SETTLE_ATTEMPTS:
                return
            driver, default = found
            legs = self._pending_legs(default)
            if legs:
                attempts += 1
                self._write_leg_profile(driver, default, legs)

        motion = self._applied_motion if isinstance(self._applied_motion, str) else None
        if found is not None and not self._sdk_tunes(found[0], motion):
            try_restore()
        self.robot.stop()
        frame_seconds = 1.0 / self.fps
        retry_frames = max(1, round(self.fps * LEG_PROFILE_RETRY_BASE_S))
        for frame in range(int(self.fps * SETTLE_SECONDS)):
            self.robot.step()
            if frame % retry_frames == 0:
                try_restore()
            self._sleep(frame_seconds)
        if found is not None and (left := self._pending_legs(found[1])):
            _LOG.error(
                "leg Profile_Velocity not restored on %d leg servo(s) after %d attempt(s); "
                "disconnecting anyway (torque off) -- the next start rewrites every leg",
                len(left),
                attempts,
            )

    def _run(self) -> None:
        frame_seconds = 1.0 / self.fps
        self._window_start = self._now()
        self._window_frames = 0
        while not self._stop_event.is_set():
            frame_start = self._now()
            tick_succeeded = self.tick()
            self._update_loop_fps(tick_succeeded, frame_start)

            elapsed = self._now() - frame_start
            self._sleep(max(0.0, frame_seconds - elapsed))
