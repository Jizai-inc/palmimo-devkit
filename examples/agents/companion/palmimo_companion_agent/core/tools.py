"""Companion-specific tool definitions layered on top of ``palmimo_sdk.agent``.

The companion agent keeps no async ``Tool`` / ``ToolBox`` vocabulary of its
own: every tool call runs through
:class:`~palmimo_sdk.agent.toolset.AgentToolSet`, synchronously against the
:class:`~palmimo_sdk.robot.Palmimo` facade (see
:mod:`palmimo_sdk.agent.tools`'s module docstring for why ``execute()`` is
sync -- ``AgentToolSet.call`` dispatches it onto a worker thread itself). The
SDK's stock tool vocabulary (``palmimo_sdk.agent.toolset.TOOL_MODELS``) is
used as-is -- including its own optional ``reason`` field on every tool (the
companion's system prompt still requires the model to fill it in every turn;
see ``core/prompts/identity.md``). This module only adds what that stock vocabulary
doesn't have:

- The composite "recipe" tools (:class:`Discover`, :class:`EyeContact`,
  :class:`Investigate`) and :class:`Rest`, none of which exist in the SDK.
- :class:`Capture`, extending the SDK's own ``CaptureTool`` with a
  ``direction`` argument (look that way first, then capture, then recenter).
- :class:`Look` / :class:`LookCenter`, extending the SDK's own tools with an
  optional gaze-hold ``seconds`` argument.
- :class:`Dance` / :class:`Nod` / :class:`HeadShake`, extending the SDK's own
  tools with a hardcoded display glyph shown for the gesture's duration.
- :class:`ShowEmoji`, extending the SDK's own tool with this robot's closed
  emoji/glyph vocabulary enforced in-schema.
- :class:`LookAtFaceTool`, a hand-rolled proportional neck-tracking loop (the
  SDK has no tracking mode of its own to delegate to).

:data:`COMPANION_TOOL_MODELS` is every tool name -> class this agent
registers onto an :class:`~palmimo_sdk.agent.toolset.AgentToolSet` on top of
the SDK's stock vocabulary (see :mod:`palmimo_companion_agent.pipeline.wiring`): a
same-name override for each SDK tool whose behavior this agent extends, plus
the composites above.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any, ClassVar, Literal, Protocol

from pydantic import Field

from palmimo_sdk.agent import PalmimoLike
from palmimo_sdk.agent.tools import CaptureTool as SdkCaptureTool
from palmimo_sdk.agent.tools import (
    DanceTool,
    Expressive,
    HeadShakeTool,
    LookCenterTool,
    LookTool,
    NodTool,
    ShowEmojiTool,
    Tool,
    ToolResult,
)
from palmimo_sdk.robot import MotionCancelled, NeckPitchDegrees, NeckYawDegrees


_log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Emoji vocabulary -- ShowEmoji's closed vocabulary (see module docstring)
# ----------------------------------------------------------------------

EmojiName = Literal[
    "CURRY",
    "RAMEN",
    "SUSHI",
    "ONIGIRI",
    "RICE",
    "PIZZA",
    "BURGER",
    "FRIES",
    "CAKE",
    "ICECREAM",
    "COFFEE",
    "BEER",
    "BOBA",
    "APPLE",
    "BANANA",
    "DONUT",
    "STRAWBERRY",
    "CHOCO",
    "DANGO",
    "GYOZA",
    "SUN",
    "MOON",
    "RAINBOW",
    "CLOUD",
    "RAIN",
    "SNOW",
    "BOLT",
    "FIRE",
    "WAVE",
    "DROPLET",
    "BLOSSOM",
    "DOG",
    "CAT",
    "PENGUIN",
    "FROG",
    "OCTOPUS",
    "FISH",
    "TURTLE",
    "STAR",
    "ROCKET",
    "GIFT",
    "TROPHY",
    "BALLOON",
    "GEM",
    "TARGET",
    "CAR",
    "HOUSE",
    "ROCK",
    "SCISSORS",
    "PAPER",
    "CAMERA",
    "GLOBE",
    "BATTERY",
    "GEAR",
    "CLOCK",
    "MAP",
    "LIGHTBULB",
    "COMPASS",
    "TELESCOPE",
    "PLANET",
    "DNA",
    "MICROSCOPE",
    "SATELLITE",
    "LAPTOP",
    "TESTTUBE",
    "COMET",
    "UFO",
    "PLANE",
    "TRAIN",
    "BIKE",
    "BUTTERFLY",
    "DINO",
    "DOLPHIN",
    "GUITAR",
    "PALETTE",
    "BDAYCAKE",
    "TENT",
    "GAME",
    "DICE",
    "GRADCAP",
]


# ----------------------------------------------------------------------
# Small shared helpers
# ----------------------------------------------------------------------


def _capture(robot: PalmimoLike) -> ToolResult:
    """Grab one frame via the SDK's own ``CaptureTool`` (no direction, no VLM).

    Shared by :class:`Capture` and the composite recipes below so the
    JPEG-encode logic lives in exactly one place (the SDK's). The VLM
    description is intentionally NOT produced here -- see
    :mod:`.dispatch`, which describes ``ToolResult.images`` after any tool
    call returns one.
    """
    return SdkCaptureTool().execute(robot)


def _motion_and_settle(robot: PalmimoLike, start: Callable[[], None], seconds: float) -> None:
    """Set a motion and stream *seconds* of it, stopping even if the run raises.

    The setters only switch the engine's target motion, so a bare call from
    outside a running loop never reaches the driver -- and a run that fails
    partway must still stop, or the robot keeps gesturing forever.
    """
    start()
    try:
        robot.run(seconds=seconds)
    finally:
        robot.stop()


def _look_and_settle(robot: PalmimoLike, pitch: float, yaw: float, seconds: float) -> None:
    """``robot.look(pitch, yaw)`` then stream *seconds* of frames so it actually moves.

    Mirrors :class:`~palmimo_sdk.agent.tools.LookTool`'s own pattern: ``look()``
    only sets an interpolation target, so a bare call from outside an
    already-running loop would never reach the driver.
    """
    robot.look(pitch, yaw)
    robot.run(seconds=seconds)


# ======================================================================
# Look / look_center -- SDK tools extended with an optional gaze-hold
# ======================================================================

# How long look/look_center streams frames when no explicit hold is asked
# for -- the same quick settle the SDK's own LookTool uses.
_LOOK_DEFAULT_HOLD_SECONDS = 0.6


class Look(LookTool):
    """SDK look + an optional gaze-hold duration.

    Every other motion tool takes ``seconds``, and the model reliably
    generalizes that onto ``look`` too -- real-robot runs showed repeated
    ``seconds`` validation rejections on an SDK LookTool that doesn't have
    the field. Accepting it (as "hold the gaze there this long") turns that
    hallucination into a useful knob instead of an error round-trip.
    """

    seconds: float | None = Field(
        default=None,
        gt=0,
        le=10,
        description="How long to hold the gaze there, in seconds. Null (default) is a quick glance.",
    )

    def _act(self, robot: PalmimoLike) -> ToolResult:
        robot.look(pitch=NeckPitchDegrees(self.pitch), yaw=NeckYawDegrees(self.yaw))
        hold = self.seconds if self.seconds is not None else _LOOK_DEFAULT_HOLD_SECONDS
        robot.run(seconds=hold)
        note = f" (held {self.seconds:g}s)" if self.seconds is not None else ""
        return ToolResult(text=f"looking at pitch={self.pitch:g} deg, yaw={self.yaw:g} deg{note}")


class LookCenter(LookCenterTool):
    """SDK look_center + the same optional hold as :class:`Look`."""

    seconds: float | None = Field(
        default=None,
        gt=0,
        le=10,
        description="How long to keep gazing straight ahead, in seconds. Null (default) is a quick recenter.",
    )

    def _act(self, robot: PalmimoLike) -> ToolResult:
        robot.look_center()
        hold = self.seconds if self.seconds is not None else _LOOK_DEFAULT_HOLD_SECONDS
        robot.run(seconds=hold)
        return ToolResult(text="looking center")


#: Response glyphs the display also understands, on top of the topic emoji in
#: :data:`EmojiName` -- kept as a separate Literal so each list stays a plain
#: enumeration of its own kind (topic emoji vs. reaction glyph).
GlyphName = Literal["MARU", "BATSU", "HATENA", "BIKKURI", "GROOVE"]


class ShowEmoji(ShowEmojiTool):
    """SDK show_emoji with the display's closed vocabulary enforced in-schema.

    The SDK deliberately leaves ``name`` an open string (firmware may know
    more names than its docstring lists); this robot's firmware vocabulary
    is known exactly, and real runs showed the model inventing plausible
    names (``WATER_WAVE`` for ``WAVE``) that only fail at the display. The
    Literal turns those into schema-level choices the model picks correctly.
    """

    # Narrowing the SDK's mutable `name_: str` field is invariance-unsound in
    # principle (pyright flags it) but safe here: tools are built once from
    # LLM args, validated, executed, and discarded -- never reassigned.
    name_: EmojiName | GlyphName = Field(  # pyright: ignore[reportIncompatibleVariableOverride]
        ...,
        alias="name",
        description="Emoji/symbol name to show.",
    )


# ======================================================================
# Motion + auto-glyph tools -- reuse the SDK tool's own execute() for the
# actual motion (pacing / settle / cancellation), just bracket it with a
# hardcoded display glyph.
# ======================================================================


class Dance(Tool):
    """Sway the body rhythmically while showing the GROOVE dance face.

    No ``face`` argument (unlike every other motion tool): GROOVE is shown
    unconditionally for the whole dance, so any requested face would just be
    overwritten. ``say`` IS supported -- the inner SDK
    :class:`~palmimo_sdk.agent.tools.DanceTool` (an ``Expressive``) receives
    it and runs the speech concurrently with the motion, so the robot can
    talk while it dances.
    """

    name: ClassVar[str] = "dance"
    description: ClassVar[str] = DanceTool.description
    long_running: ClassVar[bool] = True

    seconds: float = Field(gt=0, le=15, description="Dance duration. 4-6s typical; vary it each time.")
    say: str | None = Field(
        default=None,
        description=("Something to say while dancing (short, concurrent speech). Null (default) says nothing."),
    )

    def execute(self, robot: PalmimoLike) -> ToolResult:
        try:
            robot.set_expression("GROOVE", hold_ms=max(int(self.seconds * 1000), 1))
        except Exception:
            _log.warning("set_expression(GROOVE) failed; continuing dance", exc_info=True)
        return DanceTool(seconds=self.seconds, say=self.say).execute(robot)


class Nod(Expressive):
    """Nod "yes", showing the MARU glyph for the gesture's duration."""

    name: ClassVar[str] = "nod"
    description: ClassVar[str] = NodTool.description
    long_running: ClassVar[bool] = True

    seconds: float = Field(gt=0, le=15, description="Duration; the gesture itself completes in ~1.5s. 2-3s typical.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        try:
            robot.set_expression("MARU", hold_ms=max(int(self.seconds * 1000), 1))
        except Exception:
            _log.warning("set_expression(MARU) failed; continuing gesture", exc_info=True)
        # A bare (face=None, say=None) SDK NodTool: its own Expressive.execute()
        # reduces to just the underlying motion, so this is equivalent to
        # _run_motion(robot, robot.nod, seconds) without reaching into that
        # SDK-internal helper directly.
        result = NodTool(seconds=self.seconds).execute(robot)
        if self.face is not None:
            try:
                robot.set_expression(self.face)
            except Exception:
                _log.warning("set_expression(%s) after MARU failed", self.face, exc_info=True)
        return result


class HeadShake(Expressive):
    """Shake the head "no", showing the BATSU glyph for the gesture's duration."""

    name: ClassVar[str] = "head_shake"
    description: ClassVar[str] = HeadShakeTool.description
    long_running: ClassVar[bool] = True

    seconds: float = Field(gt=0, le=15, description="Duration; the gesture itself completes in ~1.3s. 2-3s typical.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        try:
            robot.set_expression("BATSU", hold_ms=max(int(self.seconds * 1000), 1))
        except Exception:
            _log.warning("set_expression(BATSU) failed; continuing gesture", exc_info=True)
        result = HeadShakeTool(seconds=self.seconds).execute(robot)
        if self.face is not None:
            try:
                robot.set_expression(self.face)
            except Exception:
                _log.warning("set_expression(%s) after BATSU failed", self.face, exc_info=True)
        return result


# ======================================================================
# Capture -- SDK CaptureTool + an optional direction
# ======================================================================

#: Direction -> (pitch, yaw) the neck looks toward before grabbing a frame.
#: Pitch: negative = up. Yaw: negative = left, positive = right.
_CAPTURE_DIRECTIONS: dict[str, tuple[float, float]] = {
    "left": (0.0, -0.6),
    "right": (0.0, 0.6),
    "up": (-0.6, 0.0),
    "down": (0.5, 0.0),
}
#: Seconds the neck is given to reach a capture direction / return to center.
#: Short on purpose: capture() is meant to be a quick glance.
_CAPTURE_LOOK_SECONDS = 0.3


class Capture(SdkCaptureTool):
    """Look around and observe what's there (this robot's own act of looking, not a photo request).

    Extends the SDK's own ``CaptureTool`` with an optional ``direction``: the
    neck turns that way first, then the frame is captured (via the SDK
    tool's own ``execute()``), then the neck returns to center in a
    ``finally`` -- so a mid-motion cancellation still recenters.
    """

    name: ClassVar[str] = "capture"
    description: ClassVar[str] = (
        SdkCaptureTool.description + ' If someone asks to be photographed ("take my picture"), respond with '
        "show_emoji(name='CAMERA') instead of this tool -- capture is for the robot's own "
        "curiosity, not a reply to a photo request."
    )
    long_running: ClassVar[bool] = True

    direction: Literal["center", "left", "right", "up", "down"] = Field(
        default="center",
        description="Direction to look before capturing. 'center' observes without moving the neck.",
    )

    def execute(self, robot: PalmimoLike) -> ToolResult:
        moved = self.direction != "center"
        if not moved:
            return super().execute(robot)
        pitch, yaw = _CAPTURE_DIRECTIONS[self.direction]
        try:
            _look_and_settle(robot, pitch, yaw, _CAPTURE_LOOK_SECONDS)
            result = super().execute(robot)
        finally:
            robot.look_center()
            robot.run(seconds=_CAPTURE_LOOK_SECONDS)
        return ToolResult(
            text=f"(looking {self.direction}) {result.text}", images=result.images, is_error=result.is_error
        )


# ======================================================================
# Rest (do nothing on purpose)
# ======================================================================


class Rest(Expressive):
    """Sit still for a moment. Use only when resting has a positive purpose of its own.

    Good reasons: savoring an observation, letting excitement settle, catching
    a breath after a big motion. NOT a good reason: "nothing else to do" or
    "not sure what's next" -- for those, use discover / body_tilt / look
    instead. Calling rest twice in a row reads as the agent going idle, so
    avoid that.
    """

    name: ClassVar[str] = "rest"
    description: ClassVar[str] = (
        "Sit still for a moment. Use only when resting has a positive purpose of its own "
        "(savoring an observation, letting excitement settle, catching a breath). Not a good "
        "reason: nothing else to do -- use discover / body_tilt / look instead."
    )
    long_running: ClassVar[bool] = True

    seconds: float = Field(gt=0, le=15, description="Seconds to rest. 2-4s; keep it short.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        # A bare time.sleep() streams no frames, so cancel() -- which is only
        # ever observed at a frame boundary -- had nothing to interrupt until
        # the whole sleep finished. stop() (IDLE) then run(seconds=...)
        # streams IDLE frames instead, giving a mid-rest cancel() a frame
        # boundary to land on; MotionCancelled then propagates through the
        # SDK's own cancellation path exactly like any other motion tool.
        robot.stop()
        robot.run(seconds=self.seconds)
        return ToolResult(text="rested for a moment" + (f" ({self.reason})" if self.reason else ""))


#: The head-shake that ends wake_up -- long enough to read as shaking off
#: sleep, short enough not to look like a "no".
_WAKE_SHAKE_SECONDS = 1.2


class Sleep(Expressive):
    """Go to sleep: settle down, go limp, show the sleeping face. Stays awake to the ear.

    A mode, not a gesture -- unlike every other tool here it does not end on
    its own. The robot stays like this until `wake_up`, and goes on listening
    the whole time, so being asked to wake is what ends it.

    Overrides the SDK's stock ``sleep`` (also ``long_running=False``, for the
    same reason). ``_act`` here is a bare ``robot.sleep()`` + a face change,
    exactly like the stock tool's own ``robot.sleep()`` -- it never calls
    :func:`_motion_and_settle` or any other ``run()``-driven loop, so there is
    no cancel-polling ``run()`` call for ``long_running=True`` to describe.
    ``Palmimo.sleep()`` drives its own plain ``time.sleep()`` glide that never
    takes a cancel snapshot or checks one, so there is no ``MotionCancelled``
    for a race to ever land on -- marking this ``True`` would instead mislead
    a caller like :func:`~.dispatch._run_cancellable`, which would consume a
    barge-in's cancel() as "handled" while the sleep glide finishes on its
    own, silently swallowing the interrupt instead of surfacing it. Contrast
    with :class:`WakeUp` below, whose ``_act`` DOES loop through
    :func:`_motion_and_settle`'s own cancel-polling ``run()`` calls and so
    stays ``long_running=True``.
    """

    name: ClassVar[str] = "sleep"
    description: ClassVar[str] = (
        "Go to sleep: settle to a resting pose, go limp, and show the sleeping face. "
        "Use when asked to sleep, rest properly, or go quiet for a while. The robot keeps "
        "listening and stays asleep until wake_up is called, so only use it when the person "
        "means a real pause, not a two-second breather (that is rest)."
    )
    long_running: ClassVar[bool] = False

    def _act(self, robot: PalmimoLike) -> ToolResult:
        robot.sleep()
        # After the face, not before: sleep() takes a moment to settle, and the
        # sleeping face appearing while the robot is still moving looks wrong.
        robot.set_expression("SLEEP")
        return ToolResult(text="went to sleep" + (f" ({self.reason})" if self.reason else ""))


class WakeUp(Expressive):
    """Wake from sleep: come to, stretch, shake it off, then be awake.

    Deliberately unhurried. Snapping upright and answering in the same breath
    reads as never having been asleep, so the sleepiness is worn off on the
    way out: the drowsy face comes back first, then the stretch, then a shake
    of the head. The reply belongs to the turn after this one.

    Overrides the SDK's stock ``wake_up`` (``long_running=False`` there, same
    reasoning as :class:`Sleep`'s stock counterpart -- ``robot.wake()`` alone
    is an uncancellable plain glide). Unlike :class:`Sleep` above, THIS
    override stays ``long_running=True``: its ``_act`` loops through
    :func:`_motion_and_settle`'s own ``run()`` calls during the stretch/shake,
    which does poll cancellation the normal way, so a barge-in mid-wake is a
    real ``MotionCancelled`` for a caller to race against -- unlike the bare
    ``robot.sleep()`` / ``robot.wake()`` calls this class and :class:`Sleep`
    both start with.
    """

    name: ClassVar[str] = "wake_up"
    description: ClassVar[str] = (
        "Wake up from sleep: rise back to a neutral pose, stretch, and shake off the sleepiness. "
        "Use when spoken to after having gone to sleep. Takes a few seconds on purpose -- do not "
        "speak in the same turn, answer once this has finished."
    )
    long_running: ClassVar[bool] = True

    seconds: float = Field(
        default=2.5, gt=0, le=10, description="Length of the stretch. 2-3s reads as waking up; longer drags."
    )

    def _act(self, robot: PalmimoLike) -> ToolResult:
        robot.wake()
        # Drowsy first: the stretch means nothing if it already looks awake.
        robot.set_expression("SLEEPY")
        _motion_and_settle(robot, robot.stretch, self.seconds)
        _motion_and_settle(robot, robot.head_shake, _WAKE_SHAKE_SECONDS)
        robot.set_expression("HAPPY", hold_ms=1200)
        return ToolResult(text="woke up and stretched" + (f" ({self.reason})" if self.reason else ""))


# ======================================================================
# Composite tools: a few primitives bundled into one named "recipe"
# ======================================================================


class Discover(Expressive):
    """Glance around, then take a good look -- for "let me see what's around"."""

    name: ClassVar[str] = "discover"
    description: ClassVar[str] = (
        "Glance around, then take a good look. Use for 'let me see what's around'. "
        "'left'/'right' glance the opposite way first, then look that way; 'up' looks up; "
        "'around' (no particular direction) picks left/right/up at random each time."
    )
    long_running: ClassVar[bool] = True

    direction: Literal["around", "left", "right", "up"] = Field(default="around", description="Direction to explore.")
    seconds: float = Field(
        gt=0, le=15, description="Duration of the head movement portion (capture adds its own time). 3-5s typical."
    )

    def _act(self, robot: PalmimoLike) -> ToolResult:
        observe_pitch = -0.7  # avoid looking down at the robot's own base

        direction = self.direction
        if direction == "around":
            direction = random.choice(["left", "right", "up"])

        if direction == "left":
            t_glance, t_observe = self.seconds * 0.25, self.seconds * 0.75
            _look_and_settle(robot, 0.0, random.uniform(0.2, 0.4), t_glance)
            _look_and_settle(robot, observe_pitch, -random.uniform(0.5, 0.9), t_observe)
            obs = _capture(robot)
            label = "(glanced around to the left)"
        elif direction == "right":
            t_glance, t_observe = self.seconds * 0.25, self.seconds * 0.75
            _look_and_settle(robot, 0.0, -random.uniform(0.2, 0.4), t_glance)
            _look_and_settle(robot, observe_pitch, random.uniform(0.5, 0.9), t_observe)
            obs = _capture(robot)
            label = "(glanced around to the right)"
        else:  # "up"
            t_glance, t_observe = self.seconds * 0.2, self.seconds * 0.8
            _look_and_settle(robot, random.uniform(0.2, 0.4), 0.0, t_glance)
            _look_and_settle(robot, -1.0, 0.0, t_observe)
            obs = _capture(robot)
            label = "(looked up)"
        return ToolResult(text=f"{label} {obs.text}", images=obs.images, is_error=obs.is_error)


#: Face positions eye_contact accepts, matching a VLM observation's own
#: "face is in the <upper/middle/lower> <left/center/right>" wording.
EyeContactPosition = Literal[
    "upper-left",
    "upper-center",
    "upper-right",
    "middle-left",
    "middle-center",
    "middle-right",
    "lower-left",
    "lower-center",
    "lower-right",
]


class EyeContact(Expressive):
    """Use the neck alone to meet someone's gaze, from a VLM-reported face position (no observing)."""

    name: ClassVar[str] = "eye_contact"
    description: ClassVar[str] = (
        "Use the neck alone to meet someone's gaze, from a VLM-reported face position "
        "(e.g. 'upper-right', 'middle-center'). 'middle-center' means eye contact is already "
        "made, so the neck stays put."
    )
    long_running: ClassVar[bool] = True

    face_position: EyeContactPosition = Field(
        description="The face's on-screen position, as a VLM observation reports it."
    )
    seconds: float = Field(gt=0, le=15, description="Seconds to spend moving the neck. 2-3s typical.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        v, h = self.face_position.split("-")
        pitch_map = {"upper": -1.0, "middle": 0.0, "lower": random.uniform(0.3, 0.5)}
        yaw_map = {"left": -random.uniform(0.6, 1.0), "center": 0.0, "right": random.uniform(0.6, 1.0)}
        pitch, yaw = pitch_map[v], yaw_map[h]
        if v == "middle" and h == "center":
            return ToolResult(text="(already making eye contact)")
        _look_and_settle(robot, pitch, yaw, self.seconds)
        return ToolResult(text=f"(made eye contact, pitch={pitch}, yaw={yaw})")


class Investigate(Expressive):
    """Tilt the head, then peer over toward something interesting and observe it."""

    name: ClassVar[str] = "investigate"
    description: ClassVar[str] = "Tilt the head, then peer over toward something interesting and observe it."
    long_running: ClassVar[bool] = True

    yaw: float = Field(ge=-1.0, le=1.0, description="Yaw to peer toward (+1=right, -1=left). 0 peers straight ahead.")
    seconds: float = Field(gt=0, le=15, description="Duration of tilt + look + look_center combined. 4-6s typical.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        t_tilt = self.seconds * 0.25
        t_look = self.seconds * 0.35
        t_recenter = self.seconds * 0.20
        peek_pitch = -random.uniform(0.6, 0.8)
        # try/finally so a MotionCancelled raised at ANY point in this
        # sequence -- most notably mid body_tilt, before the plain stop()
        # below ever runs -- still leaves the facade idle rather than parked
        # in Motion.BODY_TILT (stop() is idempotent, so this costs nothing on
        # the normal path).
        try:
            robot.body_tilt()
            robot.run(seconds=t_tilt)
            robot.stop()
            _look_and_settle(robot, peek_pitch, self.yaw, t_look)
            obs = _capture(robot)
            robot.look_center()
            robot.run(seconds=t_recenter)
        finally:
            robot.stop()
        return ToolResult(
            text=f"(tilted its head and peered over) {obs.text}", images=obs.images, is_error=obs.is_error
        )


# ======================================================================
# look_at_face -- hand-rolled proportional neck tracking (no SDK equivalent)
# ======================================================================


class FaceLocatorLike(Protocol):
    """The subset of :class:`~palmimo_companion_agent.core.vision.FaceLocator` :class:`LookAtFaceTool` needs."""

    def locate(self, frame: Any) -> tuple[float, float] | None: ...


#: Marker substring :class:`LookAtFaceTool`'s observation carries when it
#: tracked a face to completion (not lost, not interrupted) -- the same
#: signal :class:`~palmimo_companion_agent.core.reflexes.ReflexEngine` looks for
#: before playing its "noticed you" reaction.
LOOK_AT_FACE_SEEN_MARK = "saw a face"

# Face-tracking control loop cadence: robot.run(steps=1) already paces each
# iteration to the facade's own fps (and raises MotionCancelled at that frame
# boundary if cancelled), so this only bounds how long a single camera.latest()
# poll blocks -- not the loop's own rate.
_FACE_TRACK_PERIOD = 1.0 / 15.0
#: How much of the normalized face-center error to fold into the neck target
#: each update -- deliberately soft so the loop doesn't ring.
_FACE_TRACK_GAIN = 0.6
#: Normalized face-center error below which a tick makes no target update
#: (guards against a detector's steady-state centering bias slowly drifting
#: the neck toward a servo limit over a long track).
_FACE_TRACK_DEADBAND = 0.04
#: Seconds spent gliding the neck back to center once tracking ends.
_FACE_TRACK_RECENTER_SECONDS = 0.3


class LookAtFaceTool(Tool):
    """Track the located face with the neck until *seconds* elapse or it is lost.

    No SDK tracking mode exists to delegate this to: ``Palmimo.look()`` only
    sets an instantaneous target. This hand-rolls a plain proportional
    control loop, calling ``Palmimo.look()`` directly each tick and pacing
    via ``Palmimo.run(steps=1)`` -- which both streams the frame and checks
    :meth:`~palmimo_sdk.robot.Palmimo.cancel` at that frame boundary, so a
    ``cancel()`` mid-track is caught the same way any other long-running
    tool's motion is (see :mod:`palmimo_sdk.agent.toolset`).

    The face locator is shared, hardware-scoped infrastructure (a MediaPipe
    model instance), not a per-call LLM argument, so it is injected via
    :meth:`set_face_locator` (a classmethod, called once from
    :mod:`palmimo_companion_agent.pipeline.wiring`) rather than a pydantic field.
    """

    name: ClassVar[str] = "look_at_face"
    description: ClassVar[str] = (
        "Track a detected face with the neck for a while. Use to hold attention on someone who was just noticed."
    )
    long_running: ClassVar[bool] = True

    seconds: float = Field(gt=0, le=15, description="How long to track for, at most. 3-5s typical.")
    lost_grace: float = Field(
        default=1.0, ge=0, le=10, description="Seconds to tolerate losing the face before giving up."
    )

    _face_locator: ClassVar[FaceLocatorLike | None] = None

    @classmethod
    def set_face_locator(cls, locator: FaceLocatorLike | None) -> None:
        """Inject the shared :class:`FaceLocatorLike` this tool polls (wiring-only)."""
        cls._face_locator = locator

    def execute(self, robot: PalmimoLike) -> ToolResult:
        camera = robot.camera
        locator = type(self)._face_locator
        if camera is None or locator is None:
            return ToolResult(text="no camera attached; could not look for a face")
        pitch, yaw = 0.0, 0.0
        saw_face = False
        seen_at: float | None = None
        start = time.monotonic()
        # A checkpoint taken once, before the loop starts, rather than
        # relying solely on robot.run(steps=1)'s own entry snapshot each
        # iteration: run(steps=1) snapshots FRESH at its own entry, so a
        # cancel() delivered while the locator is processing (outside
        # run()) would otherwise be invisible -- the next iteration's
        # run(steps=1) entry postdates it and never sees it as "after
        # entry". raise_if_cancelled(checkpoint) below catches it at the
        # top of the very next iteration instead.
        checkpoint = robot.cancel_checkpoint()
        try:
            while True:
                now = time.monotonic()
                if now - start >= self.seconds:
                    break
                robot.raise_if_cancelled(checkpoint)
                frame = camera.latest(timeout=_FACE_TRACK_PERIOD)
                loc = locator.locate(frame) if frame is not None else None
                if loc is not None:
                    saw_face = True
                    seen_at = now
                    cx, cy = loc
                    ex, ey = cx - 0.5, cy - 0.5
                    if abs(ex) >= _FACE_TRACK_DEADBAND:
                        yaw = max(-1.0, min(1.0, yaw + ex * _FACE_TRACK_GAIN))
                    if abs(ey) >= _FACE_TRACK_DEADBAND:
                        pitch = max(-1.0, min(1.0, pitch + ey * _FACE_TRACK_GAIN))
                    robot.look(pitch, yaw)
                elif seen_at is not None and now - seen_at >= self.lost_grace:
                    break
                robot.run(steps=1)
        except MotionCancelled:
            robot.look_center()
            robot.run(seconds=_FACE_TRACK_RECENTER_SECONDS)
            raise
        robot.look_center()
        robot.run(seconds=_FACE_TRACK_RECENTER_SECONDS)
        if not saw_face:
            return ToolResult(text="tried to look at a face but none was found")
        return ToolResult(text=LOOK_AT_FACE_SEEN_MARK)


def make_look_at_face_tool(locator: FaceLocatorLike | None) -> type[LookAtFaceTool]:
    """Build a fresh :class:`LookAtFaceTool` subclass bound to *locator*.

    :meth:`LookAtFaceTool.set_face_locator` mutates a ``ClassVar`` on the
    shared base class -- fine for a single process with one runtime, but two
    :class:`~palmimo_companion_agent.pipeline.wiring.Runtime`\\ s built in the same
    process (e.g. two tests, or an embedding app running more than one) would
    silently clobber each other's locator through it. This instead returns a
    brand-new subclass carrying its own ``_face_locator``, so each
    :func:`~palmimo_companion_agent.pipeline.wiring.build_runtime` call gets an
    independent tool class to register. Same LLM-facing ``name`` (and class
    name, for readability in stack traces / registries) as
    :class:`LookAtFaceTool` -- only the locator differs.
    """

    class _LookAtFaceTool(LookAtFaceTool):
        _face_locator: ClassVar[FaceLocatorLike | None] = locator

    _LookAtFaceTool.__name__ = LookAtFaceTool.__name__
    _LookAtFaceTool.__qualname__ = LookAtFaceTool.__qualname__
    return _LookAtFaceTool


# ======================================================================
# Registry -- every tool name this agent registers onto an AgentToolSet
# ======================================================================

#: Every tool this agent registers on top of the SDK's stock vocabulary --
#: a same-name override (adding behavior) for each SDK tool this agent
#: extends, plus the composites this agent adds that have no SDK equivalent.
#: `creep` / `wave` are deliberately not exposed (see
#: :mod:`palmimo_companion_agent.pipeline.wiring`, which excludes them from the
#: underlying AgentToolSet before these are registered on top of it).
COMPANION_TOOL_MODELS: dict[str, type[Tool]] = {
    cls.name: cls
    for cls in (
        Dance,
        Nod,
        HeadShake,
        Look,
        LookCenter,
        Capture,
        Discover,
        EyeContact,
        Investigate,
        Rest,
        Sleep,
        WakeUp,
        ShowEmoji,
        LookAtFaceTool,
    )
}


__all__ = [
    "COMPANION_TOOL_MODELS",
    "LOOK_AT_FACE_SEEN_MARK",
    "EmojiName",
    "LookAtFaceTool",
    "make_look_at_face_tool",
]
