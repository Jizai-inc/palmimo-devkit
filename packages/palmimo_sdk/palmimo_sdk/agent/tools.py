# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""LLM-callable Palmimo actions: :class:`Tool`, :class:`ToolResult`, and the
concrete movement / gesture / sleep-wake / neck / face / voice / vision tools.

Each :class:`Tool` subclass is a pydantic model whose FIELDS are the
arguments an LLM fills in (validated by pydantic on construction), and whose
``name`` / ``description`` ClassVars are what an LLM tool-calling API is told
about the tool. :meth:`Tool.execute` maps a validated call onto the
synchronous, blocking :class:`~palmimo_sdk.robot.Palmimo` facade and reports
what happened as a short, human-readable observation string for the LLM to
read back -- never by raising, so a driving agent loop can keep going even
when a peripheral (display / speaker / camera) is not attached.

Movement and gesture tools all follow the same blocking pattern: set the
motion, ``robot.run(seconds=...)`` it in real time, then ``robot.stop()`` so
the robot doesn't keep walking/gesturing after the tool call returns. The
``run()``/``stop()`` pair goes through :func:`_run_motion`, which wraps
``run()`` in ``try/finally`` so a mid-motion exception (e.g. a driver write
failure) still stops the robot instead of leaving it walking/gesturing
forever. On a NORMAL (non-raising) return, :func:`_run_motion` also streams
:data:`_STANCE_SETTLE_SECONDS` of IDLE after ``stop()`` -- ``stop()`` alone
only sets the target motion, it doesn't drive any frames, so without this
extra settle the legs can be left frozen mid-stride (e.g. mid-air) at
whatever pose ``run()`` last commanded rather than back on a stance. This is
skipped after an exception (the settle only runs after the ``try/finally``,
not inside it): a driver bus that just failed a write should not be pushed
with more commands right away. It never touches the neck's look target --
see :func:`_run_motion` for why ``return_to_neutral()`` is not used here.

Neck tools (``look`` / ``look_center``) and :class:`StopTool` set a target on
the facade (mirroring :meth:`Palmimo.look`'s own "smoothly interpolated each
step" contract) and then ``run()`` it for a short, fixed settle window
(:data:`_NECK_SETTLE_SECONDS`) themselves -- setting the target alone does
NOT move anything; :meth:`Palmimo.look`/:meth:`Palmimo.stop` only update
internal state, and frames only reach the driver inside
:meth:`Palmimo.step`/:meth:`Palmimo.run`. Without that self-contained
``run()``, a single tool call issued outside of an already-running
step/run loop would report "looking at ..." while the neck never actually
moved.
"""

from __future__ import annotations

import base64
import contextlib
from collections.abc import Callable
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from palmimo_sdk.engine import MotionEngine
from palmimo_sdk.robot import MotionCancelled, NeckPitchDegrees, NeckYawDegrees

from .receiver import PalmimoLike, _SpeechHandleLike


class ToolResult:
    """Outcome of one tool call: an observation string plus optional images.

    ``text`` is what the LLM reads back to decide its next step. ``images``
    are raw JPEG bytes (only :class:`CaptureTool` produces any today) that a
    chat client attaches as vision content -- see :meth:`to_openai_messages`.

    ``is_error`` defaults to ``False`` and usually stays that way: a
    descriptive result like "no camera attached" (:class:`CaptureTool`) or "no
    speaker attached" (:class:`SayTool`) is a normal, successful observation
    for the LLM to read and react to, not a protocol-level failure. But an
    individual :class:`Tool`'s own ``execute()`` MAY set it when a
    device-level command itself fails -- e.g. :class:`SetFaceTool` /
    :class:`ShowEmojiTool` set it when the face display rejects the
    expression/emoji name (an ``ERR`` reply) or times out with no reply at
    all, since either case is a genuine command failure the LLM must not
    mistake for success. :meth:`~palmimo_sdk.agent.toolset.AgentToolSet.call`
    sets it additionally on its own failure paths (unknown tool name,
    malformed/invalid arguments, an uncaught exception from ``execute()``) --
    see that method's docstring.
    """

    def __init__(self, text: str, images: list[bytes] | None = None, *, is_error: bool = False) -> None:
        self.text = text
        self.images: list[bytes] = images if images is not None else []
        self.is_error = is_error

    def __repr__(self) -> str:
        return f"ToolResult(text={self.text!r}, images=<{len(self.images)} image(s)>, is_error={self.is_error!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ToolResult):
            return NotImplemented
        return self.text == other.text and self.images == other.images and self.is_error == other.is_error

    def to_openai_messages(self, tool_call_id: str) -> list[dict[str, Any]]:
        """OpenAI chat messages reporting this result.

        OpenAI's ``role="tool"`` message can only carry text, not images --
        function results answering a ``tool_call_id`` are text-only. The
        documented workaround (used by OpenAI's own vision + function-calling
        guides) is to answer the call with a text-only ``tool`` message and
        then follow it with an ordinary ``role="user"`` message carrying the
        image(s) as ``image_url`` data-URI parts, so the model still sees the
        picture on its very next turn. When there are no images, only the
        ``tool`` message is returned.
        """
        messages: list[dict[str, Any]] = [{"role": "tool", "tool_call_id": tool_call_id, "content": self.text}]
        if self.images:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode('ascii')}"},
                        }
                        for jpeg in self.images
                    ],
                }
            )
        return messages


class Tool(BaseModel):
    """Base class for one LLM-callable Palmimo action.

    Subclasses declare their LLM-visible arguments as pydantic fields and set
    the ``name`` / ``description`` ClassVars; :meth:`execute` does the actual
    facade call. ``extra="forbid"`` so a stray/misspelled argument from the
    LLM surfaces as a validation error (caught by
    :meth:`~palmimo_sdk.agent.toolset.AgentToolSet.call`) instead of being
    silently dropped. Also carries an optional ``reason`` field, inherited by
    every subclass, for the LLM to explain why it chose this action.
    """

    model_config = ConfigDict(extra="forbid")

    name: ClassVar[str]
    description: ClassVar[str]

    #: Optional short rationale the LLM fills in for why it is calling this
    #: tool. Display/logging only -- :meth:`execute` never reads it. Inherited
    #: by every subclass, so it shows up as an optional property in every
    #: tool's :meth:`parameters_schema`.
    reason: str | None = Field(
        default=None, description="A short reason (a few words) for choosing this action. Fill it in every time."
    )

    #: Whether this tool's ``execute()`` runs a timed, blocking motion on the
    #: facade (``robot.run(seconds=...)``, directly or via :func:`_run_motion`)
    #: rather than returning immediately. ``False`` by default.
    #:
    #: A tool with ``long_running = True`` is one a caller MAY race against an
    #: interruption -- e.g. an async toolset that calls
    #: :meth:`~palmimo_sdk.robot.Palmimo.cancel` from another task/thread and
    #: lets the in-flight :class:`~palmimo_sdk.robot.MotionCancelled` unwind
    #: this tool's ``execute()`` (:func:`_run_motion`'s ``try/finally``
    #: already stops the robot on any such exception). This flag itself does
    #: not wire up any cancellation -- it only marks which tools are safe/
    #: meaningful to race that way; the async toolset is what actually
    #: does the racing.
    long_running: ClassVar[bool] = False

    def execute(self, robot: PalmimoLike) -> ToolResult:
        raise NotImplementedError

    @classmethod
    def parameters_schema(cls) -> dict[str, Any]:
        """This tool's arguments as an LLM-ready JSON schema.

        Strips the pydantic-model-only ``title`` keys that
        ``model_json_schema()`` adds -- noise for a tool-calling API, which
        already has ``name`` for that. Recurses into nested structures
        (``properties``, ``$defs``, ``items``, ``anyOf``, ...) so a tool with
        a nested :class:`~pydantic.BaseModel` field comes out just as clean
        as a flat one -- see :func:`_strip_titles`.
        """
        schema = cls.model_json_schema()
        _strip_titles(schema)
        return schema


def _strip_titles(node: Any) -> None:
    """Recursively remove ``title`` keys from a JSON-schema-shaped object.

    ``model_json_schema()`` puts a ``title`` on the schema itself, on every
    property, and (for a tool with a nested :class:`~pydantic.BaseModel`
    field) on every entry under ``$defs`` too. Walking dicts and lists in
    place catches all of those without hard-coding schema keys like
    ``properties``/``$defs``/``items``/``anyOf`` one by one.
    """
    if isinstance(node, dict):
        node.pop("title", None)
        for value in node.values():
            _strip_titles(value)
    elif isinstance(node, list):
        for item in node:
            _strip_titles(item)


# How long _run_motion streams IDLE after a normal stop() to let the legs
# settle onto a stance pose, rather than returning with them frozen mid-gait
# (e.g. mid-air on a lifted foot) at wherever run() last left them. stop()
# itself only sets the target motion to IDLE -- like look()/look_center(), it
# does not drive any frames, so the settle has to run() it. Deliberately does
# NOT use return_to_neutral(): that also recenters the neck's look target,
# which would fight a look() the caller set moments earlier for an unrelated
# reason (this settle is about the legs, not the gaze).
_STANCE_SETTLE_SECONDS = 0.5


def _run_motion(robot: PalmimoLike, set_motion: Callable[[], None], seconds: float) -> None:
    """Set a motion, ``run(seconds=...)`` it with ``stop()`` guaranteed, then settle the stance.

    ``robot.run()`` blocks, pacing the motion in real time; if it raises
    partway through (e.g. a driver write failure), the motion must still be
    stopped so the robot doesn't keep walking/gesturing forever. The
    exception (if any) still propagates after ``stop()`` runs -- callers
    (:meth:`AgentToolSet.call`) are expected to catch it and turn it into an
    observation string for the LLM.

    On a normal (non-raising) return, an additional
    :data:`_STANCE_SETTLE_SECONDS` of IDLE is streamed AFTER the
    ``try/finally`` so the legs actually glide to a stance pose instead of
    being left wherever the timed motion stopped mid-stride -- see the
    :data:`_STANCE_SETTLE_SECONDS` comment for why this doesn't touch the
    neck.

    A :class:`~palmimo_sdk.robot.MotionCancelled` gets the same settle
    before it propagates: a cancellation usually means the agent is about
    to think (an LLM round-trip away from its next move), and hardware
    verification showed the interrupted gait otherwise leaves one tripod
    frozen mid-stride -- legs bent and loaded for seconds. A second
    cancel() during this settle abandons it (the robot is already stopped;
    the settle is best-effort comfort, not correctness). Other exceptions
    (e.g. a driver write failure) still propagate with no settle -- the bus
    may be unusable, so streaming more frames could make things worse.
    """
    set_motion()
    try:
        robot.run(seconds=seconds)
    except MotionCancelled:
        robot.stop()
        with contextlib.suppress(MotionCancelled):
            robot.run(seconds=_STANCE_SETTLE_SECONDS)
        raise
    finally:
        robot.stop()
    robot.run(seconds=_STANCE_SETTLE_SECONDS)


def _ran(verb: str, seconds: float) -> ToolResult:
    """Observation string for a set-motion -> run(seconds) -> stop tool."""
    return ToolResult(text=f"{verb} for {seconds}s")


# ======================================================================
# EXPRESSIVE MIXIN (talk / show a face while moving)
# ======================================================================

# The face-expression vocabulary an LLM may pass alongside a motion, via
# Expressive.face. Kept in sync BY HAND with SetFaceTool.description's list --
# SetFaceTool's own `name_` field stays a plain str (its whole point is
# letting the LLM name any expression the firmware knows, including ones
# added there later); this Literal exists only so a MOTION tool's optional
# `face` argument gets a closed, LLM-friendly enum in its schema instead of
# an open string.
FaceExpression = Literal["HAPPY", "EXCITED", "SURPRISE", "CURIOUS", "THINKING", "ANGRY", "SAD", "SHY", "SLEEPY", "LOVE"]

# Character cap on Expressive.say -- mirrors SayTool's own tolerance for a
# runaway thought-dump, but shorter: this is a quip said WHILE the robot is
# busy moving/gesturing, not the main event, so it is truncated (never
# rejected) well below SayTool.text's 500-char cap.
_EXPRESSIVE_SAY_MAX_CHARS = 60


class Expressive(Tool):
    """Mixin adding optional ``face`` / ``say`` arguments to a motion tool.

    Every locomotion, gesture, and neck (``look`` / ``look_center``) tool
    inherits this instead of :class:`Tool` directly, so an LLM can ask the
    robot to talk and/or change expression WHILE it moves (e.g. wave while
    saying "hi!") instead of issuing three separate tool calls. Both fields
    default to ``None`` -- an existing call with neither set behaves exactly
    as before this mixin existed; it only adds to each tool's schema, never
    changes an existing field.

    Subclasses implement :meth:`_act` (the tool's own motion, exactly what
    used to be their ``execute()``) instead of ``execute()`` directly --
    :meth:`execute` here handles the face/say choreography around it:

    1. If ``face`` is set, applies it via ``robot.set_expression(face)``
       BEFORE the action starts. A failure (no display, an ``ERR`` reply, an
       empty/timed-out reply, or a raised exception) is noted in the result
       text but never stops the action or raises -- the same "descriptive,
       not fatal" policy :class:`SetFaceTool` itself follows.
    2. If ``say`` is set, starts it via ``robot.say(say)`` BEFORE the action.
       ``Speaker.say()`` is itself non-blocking (a background
       :class:`~palmimo_sdk.io.speaker.SpeechHandle`), so this returns
       immediately and the action below actually runs concurrently with the
       speech -- "moving while talking", not "talk, then move".
    3. Runs the action (:meth:`_act`), blocking, exactly like every tool did
       before this mixin.
    4. If speech was started, joins the handle (bounded by
       :data:`_SAY_JOIN_TIMEOUT_SECONDS` -- the same short bound
       :class:`SayTool` uses, long enough to catch an immediate TTS failure,
       short enough not to add a real delay after an action that has
       typically already run several seconds) and notes any
       ``handle.error`` in the result text. A speech failure never marks the
       whole call ``is_error`` -- only the action's own outcome does.
    """

    face: FaceExpression | None = Field(
        default=None,
        description=(
            "Facial expression to show at the same time as this action (before it starts). "
            "Null (default) leaves the face unchanged."
        ),
    )
    say: str | None = Field(
        default=None,
        description=(
            "Something to say while performing this action (short, concurrent speech). "
            f"Truncated to {_EXPRESSIVE_SAY_MAX_CHARS} characters. Null (default) says nothing."
        ),
    )

    @field_validator("say", mode="after")
    @classmethod
    def _truncate_say(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("say must not be empty")
        return stripped[:_EXPRESSIVE_SAY_MAX_CHARS]

    def _act(self, robot: PalmimoLike) -> ToolResult:
        """Subclasses implement this instead of execute() -- the tool's own motion."""
        raise NotImplementedError

    def _apply_face(self, robot: PalmimoLike) -> str | None:
        """Best-effort ``set_expression(face)``; returns a note on failure, else ``None``."""
        if self.face is None:
            return None
        if robot.display is None:
            return f"face {self.face!r} not shown: no face display attached"
        try:
            reply = robot.set_expression(self.face)
        except Exception as exc:
            return f"face {self.face!r} not shown: {exc}"
        if _is_empty_display_reply(reply):
            return f"face {self.face!r} not shown: no reply from display (serial timeout?)"
        if _is_display_error_reply(reply):
            return f"face {self.face!r} not shown: display replied {reply!r}"
        return None

    def _finish_say(self, handle: _SpeechHandleLike | None) -> str | None:
        """Join a speech handle started before the action; returns a note, else ``None``."""
        if self.say is None:
            return None
        if handle is None:
            return "no speaker attached; could not speak"
        handle.join(timeout=_SAY_JOIN_TIMEOUT_SECONDS)
        if handle.is_alive():
            return f"still speaking: {self.say!r}"
        error = getattr(handle, "error", None)
        if error is not None:
            return f"speech failed: {error}"
        return f"said: {self.say!r}"

    def execute(self, robot: PalmimoLike) -> ToolResult:
        notes: list[str] = []
        face_note = self._apply_face(robot)
        if face_note is not None:
            notes.append(face_note)
        handle = robot.say(self.say) if self.say is not None else None
        say_note: str | None = None
        # _act() can raise -- most notably MotionCancelled, since a
        # long_running action's own run() may be cancelled mid-flight (see
        # Tool.long_running). Without try/finally, an exception here would
        # skip _finish_say() entirely and leak the background SpeechHandle
        # un-joined. The join itself is unconditionally guaranteed here; the
        # note it returns is only meaningful (and only used) on the success
        # path below -- on the exception path _act()'s exception propagates
        # and the note is simply discarded, since there is no result text
        # left to annotate it onto.
        try:
            result = self._act(robot)
        finally:
            say_note = self._finish_say(handle)
        if say_note is not None:
            notes.append(say_note)
        if not notes:
            return result
        return ToolResult(text=f"{result.text} ({'; '.join(notes)})", images=result.images, is_error=result.is_error)


# ======================================================================
# MOVEMENT
# ======================================================================


class ForwardTool(Expressive):
    """Walk forward. Use to approach something or close distance."""

    name: ClassVar[str] = "forward"
    description: ClassVar[str] = "Walk forward for a short, timed burst. Use to approach something or close distance."
    long_running: ClassVar[bool] = True

    seconds: float = Field(default=1.5, ge=0.5, le=10.0, description="How long to walk, in seconds.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        _run_motion(robot, robot.forward, self.seconds)
        return _ran("moved forward", self.seconds)


class BackwardTool(Expressive):
    """Walk backward. Use to retreat or back away from something."""

    name: ClassVar[str] = "backward"
    description: ClassVar[str] = "Walk backward for a short, timed burst. Use to retreat or back away from something."
    long_running: ClassVar[bool] = True

    seconds: float = Field(default=1.5, ge=0.5, le=10.0, description="How long to walk, in seconds.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        _run_motion(robot, robot.backward, self.seconds)
        return _ran("moved backward", self.seconds)


class TurnTool(Expressive):
    """Rotate in place. Use to change facing direction, e.g. to look toward something off to the side."""

    name: ClassVar[str] = "turn"
    description: ClassVar[str] = (
        "Rotate in place, left or right, without translating. Use to change facing direction, "
        "e.g. to turn toward something off to the side."
    )
    long_running: ClassVar[bool] = True

    direction: Literal["left", "right"] = Field(..., description="Which way to rotate.")
    seconds: float = Field(default=1.0, ge=0.5, le=10.0, description="How long to rotate, in seconds.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        set_motion = robot.rotate_left if self.direction == "left" else robot.rotate_right
        _run_motion(robot, set_motion, self.seconds)
        return _ran(f"turned {self.direction}", self.seconds)


class StrafeTool(Expressive):
    """Sidestep without turning. Use to shift position sideways while staying oriented the same way."""

    name: ClassVar[str] = "strafe"
    description: ClassVar[str] = (
        "Sidestep left or right without changing which way the body faces. Use to shift position "
        "sideways while staying oriented the same way."
    )
    long_running: ClassVar[bool] = True

    direction: Literal["left", "right"] = Field(..., description="Which way to sidestep.")
    seconds: float = Field(default=1.0, ge=0.5, le=10.0, description="How long to strafe, in seconds.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        set_motion = robot.strafe_left if self.direction == "left" else robot.strafe_right
        _run_motion(robot, set_motion, self.seconds)
        return _ran(f"strafed {self.direction}", self.seconds)


class CreepTool(Expressive):
    """Very slow, one-leg-at-a-time gait. Use on delicate/unstable footing where the normal gait risks a stumble."""

    name: ClassVar[str] = "creep"
    description: ClassVar[str] = (
        "Move forward with a slow, very stable one-leg-at-a-time gait. Use on delicate or uneven "
        "footing where the normal walking gait risks a stumble."
    )
    long_running: ClassVar[bool] = True

    seconds: float = Field(default=2.0, ge=0.5, le=10.0, description="How long to creep, in seconds.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        _run_motion(robot, robot.creep, self.seconds)
        return _ran("crept forward", self.seconds)


# ======================================================================
# GESTURES / PERFORMANCE MOTIONS
# ======================================================================


class DanceTool(Expressive):
    """Sway the body to music/rhythm. Use as a playful, entertaining gesture."""

    name: ClassVar[str] = "dance"
    description: ClassVar[str] = (
        "Sway the body rhythmically. Use as a playful, entertaining gesture, e.g. when asked to dance."
    )
    long_running: ClassVar[bool] = True

    seconds: float = Field(default=3.0, ge=0.5, le=15.0, description="How long to dance, in seconds.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        _run_motion(robot, robot.dance, self.seconds)
        return _ran("danced", self.seconds)


class BodyTiltTool(Expressive):
    """Tilt the body side to side. Use to express curiosity, like a dog tilting its head."""

    name: ClassVar[str] = "body_tilt"
    description: ClassVar[str] = "Tilt the body side to side. Use to express curiosity or puzzlement."
    long_running: ClassVar[bool] = True

    seconds: float = Field(default=3.0, ge=0.5, le=15.0, description="How long to tilt, in seconds.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        _run_motion(robot, robot.body_tilt, self.seconds)
        return _ran("tilted the body", self.seconds)


class PushupTool(Expressive):
    """Raise and lower the body repeatedly. Use to show off strength/energy, or as a playful gesture."""

    name: ClassVar[str] = "pushup"
    description: ClassVar[str] = (
        "Do push-ups (raise and lower the body). Use to show off strength/energy or as a playful gesture."
    )
    long_running: ClassVar[bool] = True

    seconds: float = Field(default=3.0, ge=0.5, le=15.0, description="How long to do push-ups, in seconds.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        _run_motion(robot, robot.pushup, self.seconds)
        return _ran("did pushups", self.seconds)


class WaveTool(Expressive):
    """Wave the front-right leg. Use for a friendly, one-handed greeting or goodbye."""

    name: ClassVar[str] = "wave"
    description: ClassVar[str] = (
        "Wave the front-right leg. Use for a friendly, one-handed greeting or goodbye, "
        "e.g. saying hello or bye to a person."
    )
    long_running: ClassVar[bool] = True

    seconds: float = Field(default=3.0, ge=0.5, le=15.0, description="How long to wave, in seconds.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        _run_motion(robot, robot.wave, self.seconds)
        return _ran("waved", self.seconds)


class WaveBothTool(Expressive):
    """Wave both front legs at once. Use for an enthusiastic, two-handed greeting or goodbye."""

    name: ClassVar[str] = "wave_both"
    description: ClassVar[str] = (
        "Wave both front legs at once. Use for an enthusiastic, two-handed greeting or goodbye."
    )
    long_running: ClassVar[bool] = True

    seconds: float = Field(default=3.0, ge=0.5, le=15.0, description="How long to wave, in seconds.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        _run_motion(robot, robot.wave_both, self.seconds)
        return _ran("waved both front legs", self.seconds)


class ClapTool(Expressive):
    """Clap the two front feet together. Use to applaud or celebrate something."""

    name: ClassVar[str] = "clap"
    description: ClassVar[str] = "Clap the two front feet together. Use to applaud or celebrate something."
    long_running: ClassVar[bool] = True

    seconds: float = Field(default=3.0, ge=0.5, le=15.0, description="How long to clap, in seconds.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        _run_motion(robot, robot.clap, self.seconds)
        return _ran("clapped", self.seconds)


class BowTool(Expressive):
    """Bow once: chest and head dip, hold, slow rise. Use as a greeting or a polite acknowledgment."""

    name: ClassVar[str] = "bow"
    description: ClassVar[str] = (
        "Bow once (chest and head dip, hold, slow rise). Use as a greeting or a polite acknowledgment."
    )
    long_running: ClassVar[bool] = True

    seconds: float = Field(default=3.0, ge=0.5, le=15.0, description="How long to run the bow motion, in seconds.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        _run_motion(robot, robot.bow, self.seconds)
        return _ran("bowed", self.seconds)


class StretchTool(Expressive):
    """Stretch once: wind-up crouch, rise tall, lower. Use after being idle, like waking up."""

    name: ClassVar[str] = "stretch"
    description: ClassVar[str] = (
        "Stretch once (crouch, rise tall, lower). Use after being idle for a while, like waking up."
    )
    long_running: ClassVar[bool] = True

    seconds: float = Field(default=3.0, ge=0.5, le=15.0, description="How long to run the stretch motion, in seconds.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        _run_motion(robot, robot.stretch, self.seconds)
        return _ran("stretched", self.seconds)


class NodTool(Expressive):
    """Nod the head "yes". Use to answer a yes/no question affirmatively, or to acknowledge something."""

    name: ClassVar[str] = "nod"
    description: ClassVar[str] = (
        'Nod the head "yes" (neck-only). Use to answer a yes/no question affirmatively, or to acknowledge something.'
    )
    long_running: ClassVar[bool] = True

    seconds: float = Field(default=3.0, ge=0.5, le=15.0, description="How long to run the nod motion, in seconds.")

    def _act(self, robot: PalmimoLike) -> ToolResult:
        _run_motion(robot, robot.nod, self.seconds)
        return _ran("nodded", self.seconds)


class HeadShakeTool(Expressive):
    """Shake the head "no". Use to answer a yes/no question negatively, or to express disagreement."""

    name: ClassVar[str] = "head_shake"
    description: ClassVar[str] = (
        'Shake the head "no" (neck-only). Use to answer a yes/no question negatively, or to express disagreement.'
    )
    long_running: ClassVar[bool] = True

    seconds: float = Field(
        default=3.0, ge=0.5, le=15.0, description="How long to run the head-shake motion, in seconds."
    )

    def _act(self, robot: PalmimoLike) -> ToolResult:
        _run_motion(robot, robot.head_shake, self.seconds)
        return _ran("shook the head", self.seconds)


# ======================================================================
# SLEEP / WAKE
# ======================================================================
#
# Thin wrappers over Palmimo.sleep()/wake() -- a pose-and-stiffness pair, not
# a motion (see PalmimoLike's own "Sleep / wake" section). Plain Tool, not
# Expressive: sleep/wake are not a gesture layer to choreograph a face/say
# alongside -- an app that wants a "going to sleep" expression composes it on
# top of this tool (e.g. the companion example's own override), the same way
# StopTool stays a plain Tool.


class SleepTool(Tool):
    """Go limp into a resting pose and stay asleep until wake_up is called."""

    name: ClassVar[str] = "sleep"
    description: ClassVar[str] = (
        "Go limp into a resting pose and STAY asleep until wake_up is called -- this is not a "
        "brief pause, it holds until wake_up. The robot keeps listening while asleep. Use for a "
        "real pause (e.g. told to go to sleep or take a long break), not a two-second breather."
    )
    # Deliberately left at the Tool base's default False, same reasoning as
    # StopTool: long_running marks a tool a caller MAY race against a
    # Palmimo.cancel()/MotionCancelled interruption (see robot.cancel()), but
    # Palmimo.sleep() drives its own plain time.sleep() frame loop -- it never
    # takes a cancel snapshot or checks one -- so there is no MotionCancelled
    # for a race to ever land on; the glide simply runs to completion
    # regardless. Marking this True would additionally mislead a caller like
    # the companion example's `_run_cancellable`, which would consume a
    # barge-in's cancel() as "handled" while the sleep glide finishes on its
    # own, silently swallowing the interrupt instead of surfacing it.
    long_running: ClassVar[bool] = False

    def execute(self, robot: PalmimoLike) -> ToolResult:
        robot.sleep()
        return ToolResult(text="went to sleep")


class WakeTool(Tool):
    """Rise back to neutral from sleep."""

    name: ClassVar[str] = "wake_up"
    description: ClassVar[str] = "Rise back to neutral from sleep. Use when spoken to after sleeping."
    # Deliberately left at the Tool base's default False -- same reasoning as
    # SleepTool's own comment above: Palmimo.wake() is an uncancellable plain
    # time.sleep() glide, so there is no MotionCancelled for long_running=True
    # to ever race against.
    long_running: ClassVar[bool] = False

    def execute(self, robot: PalmimoLike) -> ToolResult:
        robot.wake()
        return ToolResult(text="woke up")


# ======================================================================
# NECK
# ======================================================================

# LookTool's LLM-facing arguments are in degrees (more natural for an LLM to
# reason about than the facade's normalized [-1, 1]), passed to robot.look()
# as NeckPitchDegrees / NeckYawDegrees so Palmimo.look() does the
# degrees->normalized conversion itself (dividing by the engine's PUBLIC
# per-axis neck travel, MotionEngine.NECK_PITCH_TRAVEL_DEG /
# NECK_YAW_TRAVEL_DEG, ~26.4 deg today on both axes). The schema's declared
# range per axis is that same travel, so the LLM's stated range and the
# physical range always agree -- no more "declared 60 deg yaw, actually
# saturates around 26". A request right at the schema boundary lands exactly
# at the neck's full deflection; the value objects self-validate at
# construction (ValueError past travel) rather than saturating, so the schema
# bound and the value object's own bound must (and do) agree.
_LOOK_PITCH_MAX_DEG = MotionEngine.NECK_PITCH_TRAVEL_DEG
_LOOK_YAW_MAX_DEG = MotionEngine.NECK_YAW_TRAVEL_DEG

# How long look()/look_center()/stop() run() the neck for after setting a new
# target, so the interpolated frames actually reach the driver instead of
# just sitting in the engine's target state -- see the module docstring.
# 0.6s comfortably covers one settle from either axis's full travel at the
# engine's default neck interpolation rate; it is deliberately short (an LLM
# tool call should feel responsive, not add a multi-second pause per look).
_NECK_SETTLE_SECONDS = 0.6


class LookTool(Expressive):
    """Aim the head/gaze. Use to look toward a person, object, or direction without moving the body."""

    name: ClassVar[str] = "look"
    # Sign semantics follow the hardware-verified facade contract (2026-07):
    # positive pitch tips the chin DOWN; the yaw sign-to-direction mapping is
    # not yet verified on hardware, so the description makes no direction claim.
    description: ClassVar[str] = (
        "Aim the head/gaze in a direction. Use to look toward a person, object, or direction "
        "without moving the body. Positive pitch tips the chin down (looks down)."
    )
    # A blocking robot.run(seconds=_NECK_SETTLE_SECONDS) call underneath (see
    # _act below), same as any other timed motion tool -- not the "instant,
    # non-blocking" call the doc/guides/motion-development-guide.md guidance once
    # assumed. long_running=True marks it as one a caller may race against a
    # Palmimo.cancel()/MotionCancelled interruption, same as the movement/
    # gesture tools.
    long_running: ClassVar[bool] = True

    pitch: float = Field(
        default=0.0,
        ge=-_LOOK_PITCH_MAX_DEG,
        le=_LOOK_PITCH_MAX_DEG,
        description="Vertical look angle in degrees. Positive = chin down (look down).",
    )
    yaw: float = Field(
        default=0.0,
        ge=-_LOOK_YAW_MAX_DEG,
        le=_LOOK_YAW_MAX_DEG,
        description="Horizontal look angle in degrees.",
    )

    def _act(self, robot: PalmimoLike) -> ToolResult:
        # robot.look() only sets an internal target -- frames only reach the
        # driver inside step()/run(), so a bare look() from a single tool
        # call would silently not move the neck. run() here streams the
        # settle itself; run() does not need _run_motion's try/finally
        # since the neck target is already what we want (nothing to stop).
        # NeckPitchDegrees/NeckYawDegrees do the real-degrees -> normalized
        # conversion inside Palmimo.look() itself (see the _LOOK_*_MAX_DEG
        # comment above).
        robot.look(pitch=NeckPitchDegrees(self.pitch), yaw=NeckYawDegrees(self.yaw))
        robot.run(seconds=_NECK_SETTLE_SECONDS)
        return ToolResult(text=f"looking at pitch={self.pitch} deg, yaw={self.yaw} deg")


class LookCenterTool(Expressive):
    """Return the head/gaze to center, facing forward."""

    name: ClassVar[str] = "look_center"
    description: ClassVar[str] = (
        "Return the head/gaze to center, facing forward. Use after looking elsewhere to reset attention."
    )
    # Blocking robot.run(seconds=_NECK_SETTLE_SECONDS) underneath, same as
    # LookTool -- see that class's long_running comment.
    long_running: ClassVar[bool] = True

    def _act(self, robot: PalmimoLike) -> ToolResult:
        # See LookTool.execute: look_center() only sets the target, run()
        # actually streams the neck there.
        robot.look_center()
        robot.run(seconds=_NECK_SETTLE_SECONDS)
        return ToolResult(text="looking center")


# ======================================================================
# FACE DISPLAY
# ======================================================================


def _is_display_error_reply(reply: str | None) -> bool:
    """True when a face-display reply reports a firmware-side failure.

    The firmware's error contract (see ``FaceDisplay.set_expression`` and
    firmware/display/src/face_serial.c) is a reply that IS the ``ERR`` token
    or starts with the ``ERR `` token followed by a space -- e.g.
    ``ERR NOT_A_FACE (try LIST)`` for an unknown expression/emoji name.
    Matched by token rather than by bare prefix so a reply that merely starts
    with the letters "ERR" without being that token -- ``ERROR ...``,
    ``ERRATA ...`` -- is not misclassified as this specific firmware failure.
    Stripped and case-normalized before the check so incidental whitespace or
    casing doesn't hide a failure behind the success wording (that's the
    original bug this guards against: without it, an ``ERR ...`` reply was
    reported back to the LLM inside a "showing expression ..." sentence,
    reading as a success and misleading the tool-calling loop into thinking
    the expression was set).
    """
    if reply is None:
        return False
    normalized = reply.strip().upper()
    return normalized == "ERR" or normalized.startswith("ERR ")


def _is_empty_display_reply(reply: str | None) -> bool:
    """True when a face-display reply is empty or whitespace-only.

    ``FaceDisplay._command()`` returns ``""`` when no reply line arrives
    within its serial timeout (see io/display.py) -- a wedged/rebooting
    display MCU, or a USB-CDC hiccup that ate the reply, looks exactly like
    this rather than like a firmware ``ERR``. Without this check, an empty
    reply got embedded verbatim into a "showing expression ... (display
    replied: )" success sentence, misleading the tool-calling loop into
    thinking the expression was actually set when the display may never have
    received (or acted on) the command at all.
    """
    return reply is None or reply.strip() == ""


class SetFaceTool(Tool):
    """Show a facial expression. Use to express an emotion/reaction, e.g. happy, sad, surprised."""

    name: ClassVar[str] = "set_face"
    description: ClassVar[str] = (
        "Show a facial expression on the face display and keep it until the next expression. "
        "Use to express an emotion or reaction. Available: HAPPY, EXCITED, SURPRISE, CURIOUS, "
        "THINKING, ANGRY, SAD, SHY, SLEEPY, LOVE."
    )

    name_: str = Field(..., alias="name", description="Expression name (e.g. 'HAPPY', 'SAD', 'CURIOUS').")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def execute(self, robot: PalmimoLike) -> ToolResult:
        if robot.display is None:
            return ToolResult(text="no face display attached; could not show expression")
        # Goes through the public robot.set_expression() facade rather than
        # robot.display.set_expression() directly, matching how every other
        # tool talks to Palmimo only through its facade methods.
        reply = robot.set_expression(self.name_)
        if _is_empty_display_reply(reply):
            return ToolResult(
                text=f"failed to show expression {self.name_!r}: no reply from display (serial timeout?)",
                is_error=True,
            )
        if _is_display_error_reply(reply):
            return ToolResult(
                text=f"failed to show expression {self.name_!r}: display replied {reply!r}",
                is_error=True,
            )
        return ToolResult(text=f"showing expression {self.name_!r} (display replied: {reply})")


class ShowEmojiTool(Tool):
    """Flash an emoji/symbol on the display for a few seconds, then auto-revert to idle."""

    name: ClassVar[str] = "show_emoji"
    description: ClassVar[str] = (
        "Flash an emoji or symbol on the face display for a few seconds; it then auto-reverts "
        "to the idle face (unlike set_face, which holds). Use as a quick visual reaction or a "
        "wordless answer. Symbols: MARU (yes/correct), BATSU (no/wrong), HATENA (?), BIKKURI (!). "
        "Emoji examples: STAR, ROCKET, SUSHI, DOG, CAT, RAINBOW, GIFT, CAMERA, GAME."
    )

    name_: str = Field(..., alias="name", description="Emoji/symbol name (e.g. 'MARU', 'HATENA', 'STAR').")
    seconds: float = Field(
        default=3.0, ge=0.5, le=10.0, description="How long to show it before reverting, in seconds."
    )

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def execute(self, robot: PalmimoLike) -> ToolResult:
        if robot.display is None:
            return ToolResult(text="no face display attached; could not show emoji")
        # Same firmware contract as set_face (`EXPR <name> <ms>`): a nonzero
        # hold_ms makes the display revert to idle by itself, so there is no
        # revert bookkeeping on the host side.
        reply = robot.set_expression(self.name_, hold_ms=max(int(self.seconds * 1000), 1))
        if _is_empty_display_reply(reply):
            return ToolResult(
                text=f"failed to show emoji {self.name_!r}: no reply from display (serial timeout?)",
                is_error=True,
            )
        if _is_display_error_reply(reply):
            return ToolResult(
                text=f"failed to show emoji {self.name_!r}: display replied {reply!r}",
                is_error=True,
            )
        return ToolResult(text=f"showing emoji {self.name_!r} for {self.seconds}s (display replied: {reply})")


# ======================================================================
# VOICE
# ======================================================================


# How long SayTool waits for the SpeechHandle before giving up and reporting
# "still speaking" -- long enough to catch an immediate TTS failure (piper
# missing/misconfigured errors out well under this), short enough that a
# normal, longer utterance still returns promptly instead of blocking the
# tool-calling loop on the whole sentence.
_SAY_JOIN_TIMEOUT_SECONDS = 0.5


class SayTool(Tool):
    """Speak text out loud. Use to talk to a person."""

    name: ClassVar[str] = "say"
    description: ClassVar[str] = "Speak text out loud through the speaker. Use to talk to a person."

    text: str = Field(..., max_length=500, description="What to say, up to 500 characters.")

    def execute(self, robot: PalmimoLike) -> ToolResult:
        # robot.say() is non-blocking (mirroring set_expression's fire-and-see
        # contract): a normal-length utterance should not stall the tool loop
        # for its full duration. But a bare fire-and-forget also hides an
        # immediate TTS failure (missing/misconfigured espeak/piper) behind a
        # cheerful "speaking: ..." that never actually spoke. Splitting the
        # difference: join() briefly, just long enough for that kind of
        # immediate death to surface via SpeechHandle.error, then report
        # in-progress speech as background rather than block on it.
        handle = robot.say(self.text)
        if handle is None:
            return ToolResult(text="no speaker attached; could not speak")
        handle.join(timeout=_SAY_JOIN_TIMEOUT_SECONDS)
        if handle.is_alive():
            return ToolResult(text=f"speaking (in background): {self.text!r}")
        error = getattr(handle, "error", None)
        if error is not None:
            return ToolResult(text=f"speech failed: {error}")
        return ToolResult(text=f"spoke: {self.text!r}")


# ======================================================================
# VISION
# ======================================================================


class CaptureTool(Tool):
    """Take a photo from the head camera. Use to see what's in front of the robot."""

    name: ClassVar[str] = "capture"
    description: ClassVar[str] = "Take a photo from the head camera. Use to see what's currently in front of the robot."

    def execute(self, robot: PalmimoLike) -> ToolResult:
        if robot.camera is None:
            return ToolResult(text="no camera attached; could not capture an image")
        try:
            ok, frame = robot.camera.read()
        except Exception as exc:
            return ToolResult(text=f"failed to read from the head camera: {exc}")
        if not ok or frame is None:
            return ToolResult(text="failed to capture an image from the head camera")
        try:
            import cv2  # lazy: keeps `import palmimo_sdk.agent` usable without opencv installed

            encoded_ok, buf = cv2.imencode(".jpg", frame)
        except Exception as exc:
            return ToolResult(text=f"captured a frame but could not JPEG-encode it: {exc}")
        if not encoded_ok:
            return ToolResult(text="captured a frame but JPEG encoding failed")
        return ToolResult(text="captured image from head camera", images=[buf.tobytes()])


# ======================================================================
# STOP
# ======================================================================


class StopTool(Tool):
    """Stop all motion and recenter the gaze. Use to halt whatever the robot is currently doing."""

    name: ClassVar[str] = "stop"
    description: ClassVar[str] = (
        "Stop all motion and recenter the gaze. Use to halt whatever the robot is currently doing."
    )
    # Deliberately left at the Tool base's default False, even though this
    # also runs a blocking robot.run(seconds=_NECK_SETTLE_SECONDS) underneath
    # (like LookTool/LookCenterTool). long_running marks a tool a caller MAY
    # race against a Palmimo.cancel()/MotionCancelled interruption -- but
    # racing stop() itself against a cancellation is pointless: stop() IS
    # the cancellation-shaped tool an agent reaches for to halt an
    # in-flight action, so there is nothing meaningful left to cancel it
    # with.
    long_running: ClassVar[bool] = False

    def execute(self, robot: PalmimoLike) -> ToolResult:
        robot.stop()
        robot.look_center()
        # Same "target alone doesn't move anything" reason as LookTool: run()
        # here streams the recentering. No try/finally needed -- the motion
        # is already IDLE, so there's nothing left to stop on an exception.
        robot.run(seconds=_NECK_SETTLE_SECONDS)
        return ToolResult(text="stopped and recentered gaze")
