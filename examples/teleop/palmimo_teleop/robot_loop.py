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

from palmimo_sdk import Palmimo

from .control import resolve_motion
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

#: Sign applied to the pilot's raw stick value before `Palmimo.look()`.
#: `Palmimo.look`'s sign-to-direction mapping is itself unspecified (see its
#: docstring); these were verified against a real neck (2026-09): pitch
#: tracks the stick as-is, yaw comes out mirrored and needs the flip.
NECK_PITCH_SIGN: float = 1.0
NECK_YAW_SIGN: float = -1.0


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
        self._applied_motion: str | None | object = _UNSET

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
            motion = resolve_motion(effective.move, effective.rotate, effective.gesture)
            if motion != self._applied_motion:
                if motion is None:
                    self.robot.stop()
                else:
                    self.robot.set_motion(motion)
                self._applied_motion = motion
            self.robot.look(pitch=effective.neck_pitch * NECK_PITCH_SIGN, yaw=effective.neck_yaw * NECK_YAW_SIGN)
            self.robot.step()
        except Exception:
            self._record_tick_failure()
            return False
        with self._ok_lock:
            self._robot_ok = True
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
        """
        self.robot.stop()
        frame_seconds = 1.0 / self.fps
        for _ in range(int(self.fps * SETTLE_SECONDS)):
            self.robot.step()
            self._sleep(frame_seconds)

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
