"""palmimo_sdk.agent.tools — Tool/ToolResult schema, dispatch, and error paths."""

import base64
import subprocess
import sys
from typing import Any, ClassVar, Literal, cast

import pytest
from pydantic import BaseModel, Field, ValidationError

from palmimo_sdk import MotionCancelled, MotionEngine, Palmimo
from palmimo_sdk.agent import PalmimoLike
from palmimo_sdk.agent.tools import (
    _STANCE_SETTLE_SECONDS,
    BackwardTool,
    BodyTiltTool,
    BowTool,
    CaptureTool,
    ClapTool,
    CreepTool,
    DanceTool,
    Expressive,
    ForwardTool,
    HeadShakeTool,
    LookCenterTool,
    LookTool,
    NodTool,
    PushupTool,
    SayTool,
    SetFaceTool,
    ShowEmojiTool,
    SleepTool,
    StopTool,
    StrafeTool,
    StretchTool,
    Tool,
    ToolResult,
    TurnTool,
    WakeTool,
    WaveBothTool,
    WaveTool,
    _run_motion,
)
from palmimo_sdk.robot import NeckPitchDegrees, NeckYawDegrees


try:
    import cv2  # noqa: F401

    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


# ----------------------------------------------------------------------
# Fakes: duck-typed Palmimo-alikes that just record what was called, and
# never sleep (unlike the real facade's run(), which paces at fps).
# ----------------------------------------------------------------------


class FakeDisplay:
    def __init__(self, *, reply: str | None = None) -> None:
        self.calls: list[tuple[str, str, int]] = []
        # When set, overrides the default "OK <NAME>" success reply -- used
        # to simulate the firmware's "ERR <name> (try LIST)" failure reply.
        self._reply = reply

    def set_expression(self, name: str, hold_ms: int = 0) -> str:
        self.calls.append(("set_expression", name, hold_ms))
        return self._reply if self._reply is not None else f"OK {name.upper()}"


class FakeSpeechHandle:
    """Stand-in for palmimo_sdk.io.speaker.SpeechHandle: join()/is_alive()/error."""

    def __init__(self, *, alive: bool, error: BaseException | None = None) -> None:
        self._alive = alive
        self.error = error
        self.join_calls: list[float | None] = []

    def join(self, timeout: float | None = None) -> None:
        self.join_calls.append(timeout)

    def is_alive(self) -> bool:
        return self._alive


class FakeSpeaker:
    def __init__(self, *, handle: FakeSpeechHandle | None = None) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        # Defaults to a handle that has already finished with no error, i.e.
        # the common case where join(timeout=0.5) catches real completion.
        self._handle = handle if handle is not None else FakeSpeechHandle(alive=False)

    def say(self, text: str, lang: str | None = None) -> FakeSpeechHandle:
        self.calls.append(("say", text, lang))
        return self._handle


class FakeCamera:
    def __init__(self, ok: bool = True, frame: Any = None) -> None:
        self._ok = ok
        self._frame = frame
        self.calls: list[str] = []

    def read(self) -> tuple[bool, Any]:
        self.calls.append("read")
        return self._ok, self._frame


class FakeRobot:
    """Duck-typed Palmimo double: records every facade call in order.

    ``run(seconds=...)`` and ``stop()`` are recorded rather than executed for
    real, so movement/gesture tool tests never sleep. ``display`` /
    ``speaker`` / ``camera`` mirror the facade's peripheral properties
    (``None`` unless a fake is passed in).
    """

    def __init__(
        self,
        *,
        display: FakeDisplay | None = None,
        speaker: FakeSpeaker | None = None,
        camera: FakeCamera | None = None,
        run_raises: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.display = display
        self.speaker = speaker
        self.camera = camera
        # When set, run() records the call, then raises this instead of
        # returning -- for exercising the try/finally stop() guarantee.
        self._run_raises = run_raises

    def _record(self, *entry: Any) -> None:
        self.calls.append(entry)

    def forward(self) -> None:
        self._record("forward")

    def backward(self) -> None:
        self._record("backward")

    def rotate_left(self) -> None:
        self._record("rotate_left")

    def rotate_right(self) -> None:
        self._record("rotate_right")

    def strafe_left(self) -> None:
        self._record("strafe_left")

    def strafe_right(self) -> None:
        self._record("strafe_right")

    def creep(self) -> None:
        self._record("creep")

    def dance(self) -> None:
        self._record("dance")

    def body_tilt(self) -> None:
        self._record("body_tilt")

    def pushup(self) -> None:
        self._record("pushup")

    def wave(self) -> None:
        self._record("wave")

    def wave_both(self) -> None:
        self._record("wave_both")

    def clap(self) -> None:
        self._record("clap")

    def bow(self) -> None:
        self._record("bow")

    def stretch(self) -> None:
        self._record("stretch")

    def nod(self) -> None:
        self._record("nod")

    def head_shake(self) -> None:
        self._record("head_shake")

    def sleep(self, duration: float = 1.5, end_gain: int = 300) -> None:
        self._record("sleep")

    def wake(self, duration: float = 1.5, start_gain: int = 300) -> None:
        self._record("wake")

    def run(self, *, seconds: float | None = None, steps: int | None = None) -> None:
        self._record("run", seconds)
        if self._run_raises is not None:
            raise self._run_raises

    def stop(self) -> None:
        self._record("stop")

    def look(self, pitch: float | NeckPitchDegrees = 0.0, yaw: float | NeckYawDegrees = 0.0) -> None:
        # Mirrors the real Palmimo.look() signature: LookTool now passes
        # NeckPitchDegrees/NeckYawDegrees value objects -- the tool used to do
        # its own degrees/normalized division against a hand-picked max, which
        # desynced from the engine's real ~26 deg travel.
        self._record("look", pitch, yaw)

    def look_center(self) -> None:
        self._record("look_center")

    def set_expression(self, name: str, hold_ms: int = 0) -> str | None:
        self._record("set_expression", name, hold_ms)
        if self.display is None:
            return None
        return self.display.set_expression(name, hold_ms)

    def say(self, text: str, lang: str | None = None) -> FakeSpeechHandle | None:
        self._record("say", text)
        if self.speaker is None:
            return None
        return self.speaker.say(text, lang)


# ----------------------------------------------------------------------
# ToolResult conversion helpers
# ----------------------------------------------------------------------


def test_tool_result_defaults_to_no_images() -> None:
    result = ToolResult(text="hello")
    assert result.text == "hello"
    assert result.images == []


def test_tool_result_defaults_is_error_to_false() -> None:
    result = ToolResult(text="hello")
    assert result.is_error is False


def test_tool_result_is_error_is_a_keyword_argument() -> None:
    result = ToolResult(text="boom", is_error=True)
    assert result.is_error is True


def test_tool_result_repr_includes_is_error() -> None:
    assert "is_error=True" in repr(ToolResult(text="boom", is_error=True))
    assert "is_error=False" in repr(ToolResult(text="ok"))


def test_tool_result_eq_considers_is_error() -> None:
    assert ToolResult(text="x") == ToolResult(text="x")
    assert ToolResult(text="x") != ToolResult(text="x", is_error=True)


def test_to_openai_messages_text_only() -> None:
    result = ToolResult(text="ok")
    messages = result.to_openai_messages("call_123")
    assert messages == [{"role": "tool", "tool_call_id": "call_123", "content": "ok"}]


def test_to_openai_messages_with_images_appends_user_message() -> None:
    jpeg = b"\xff\xd8fakejpeg"
    result = ToolResult(text="saw something", images=[jpeg])
    messages = result.to_openai_messages("call_123")
    assert len(messages) == 2
    assert messages[0] == {"role": "tool", "tool_call_id": "call_123", "content": "saw something"}
    assert messages[1]["role"] == "user"
    parts = messages[1]["content"]
    assert len(parts) == 1
    assert parts[0]["type"] == "image_url"
    data_uri = parts[0]["image_url"]["url"]
    assert data_uri.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(data_uri.split(",", 1)[1]) == jpeg


# ----------------------------------------------------------------------
# Schema: parameters_schema() strips pydantic-only `title` noise.
# ----------------------------------------------------------------------


def test_parameters_schema_strips_title_keys() -> None:
    schema = ForwardTool.parameters_schema()
    assert "title" not in schema
    for prop in schema["properties"].values():
        assert "title" not in prop


def test_parameters_schema_reflects_field_constraints() -> None:
    schema = ForwardTool.parameters_schema()
    seconds_schema = schema["properties"]["seconds"]
    assert seconds_schema["minimum"] == 0.5
    assert seconds_schema["maximum"] == 10.0
    assert seconds_schema["default"] == 1.5


def test_turn_schema_has_required_direction_and_literal_enum() -> None:
    schema = TurnTool.parameters_schema()
    assert "direction" in schema["required"]
    assert schema["properties"]["direction"]["enum"] == ["left", "right"]
    assert "seconds" not in schema["required"]  # has a default


def test_set_face_schema_exposes_name_not_name_underscore() -> None:
    schema = SetFaceTool.parameters_schema()
    assert "name" in schema["properties"]
    assert "name_" not in schema["properties"]
    assert schema["required"] == ["name"]


class _NestedPayload(BaseModel):
    value: int = Field(..., description="A nested value.")


class _NestedFieldTool(Tool):
    """Test-only tool with a nested BaseModel field, to exercise recursive title stripping."""

    name: ClassVar[str] = "nested_field_test_tool"
    description: ClassVar[str] = "test only"

    payload: _NestedPayload


def test_parameters_schema_strips_title_recursively_from_nested_defs() -> None:
    schema = _NestedFieldTool.parameters_schema()
    assert "title" not in schema
    for prop in schema["properties"].values():
        assert "title" not in prop
    defs = schema.get("$defs", {})
    assert defs, "expected model_json_schema() to emit $defs for the nested model"
    for definition in defs.values():
        assert "title" not in definition
        for prop in definition.get("properties", {}).values():
            assert "title" not in prop


# ----------------------------------------------------------------------
# Tool.reason -- optional display/logging field inherited by every tool.
# ----------------------------------------------------------------------


def test_reason_appears_optional_in_schema_with_exact_description() -> None:
    schema = ForwardTool.parameters_schema()
    assert "reason" in schema["properties"]
    assert schema["properties"]["reason"]["description"] == (
        "A short reason (a few words) for choosing this action. Fill it in every time."
    )
    assert "reason" not in schema.get("required", [])


def test_reason_appears_optional_in_schema_alongside_required_args() -> None:
    """TurnTool has a required `direction` arg; `reason` must stay optional
    and not get folded into that required list."""
    schema = TurnTool.parameters_schema()
    assert "reason" in schema["properties"]
    assert "reason" not in schema["required"]
    assert "direction" in schema["required"]


def test_tool_defaults_reason_to_none_when_omitted() -> None:
    robot = FakeRobot()
    tool = ForwardTool()
    assert tool.reason is None
    result = tool.execute(cast(PalmimoLike, robot))
    assert robot.calls == [("forward",), ("run", 1.5), ("stop",), ("run", _STANCE_SETTLE_SECONDS)]
    assert result.text == "moved forward for 1.5s"


def test_tool_accepts_reason_string_and_execute_ignores_it() -> None:
    robot = FakeRobot()
    tool = ForwardTool(reason="greeting")
    assert tool.reason == "greeting"
    result = tool.execute(cast(PalmimoLike, robot))
    assert robot.calls == [("forward",), ("run", 1.5), ("stop",), ("run", _STANCE_SETTLE_SECONDS)]
    assert result.text == "moved forward for 1.5s"


# ----------------------------------------------------------------------
# Tool.long_running -- which tools a caller may race against a cancellation.
# ----------------------------------------------------------------------


def test_long_running_defaults_to_false_on_the_base_tool() -> None:
    assert Tool.long_running is False


@pytest.mark.parametrize(
    "tool_cls",
    [
        ForwardTool,
        BackwardTool,
        TurnTool,
        StrafeTool,
        CreepTool,
        DanceTool,
        BodyTiltTool,
        PushupTool,
        WaveTool,
        WaveBothTool,
        ClapTool,
        BowTool,
        StretchTool,
        NodTool,
        HeadShakeTool,
        LookTool,
        LookCenterTool,
    ],
)
def test_long_running_true_for_movement_gesture_and_neck_tools(tool_cls: type[Tool]) -> None:
    assert tool_cls.long_running is True


@pytest.mark.parametrize(
    "tool_cls",
    [SetFaceTool, ShowEmojiTool, SayTool, CaptureTool, StopTool, SleepTool, WakeTool],
)
def test_long_running_false_for_instant_tools(tool_cls: type[Tool]) -> None:
    """StopTool's own precedent: `sleep`/`wake_up` drive an uncancellable plain
    time.sleep() glide underneath (no cancel snapshot taken or checked), so
    there is no MotionCancelled for long_running=True to ever race against --
    see SleepTool/WakeTool's own long_running comments in tools.py."""
    assert tool_cls.long_running is False


# ----------------------------------------------------------------------
# Expressive mixin -- optional face/say alongside a motion/neck tool.
# ----------------------------------------------------------------------


def test_expressive_schema_adds_optional_face_and_say_without_new_required() -> None:
    """The mixin only ADDS optional properties -- an existing (pre-mixin) call
    stays valid, and the existing required fields (e.g. TurnTool.direction)
    are untouched."""
    schema = ForwardTool.parameters_schema()
    # Optional[Literal[...]] renders as anyOf: [{enum: [...]}, {type: "null"}].
    face_variants = schema["properties"]["face"]["anyOf"]
    assert {"HAPPY", "EXCITED", "SURPRISE", "CURIOUS", "THINKING", "ANGRY", "SAD", "SHY", "SLEEPY", "LOVE"} == set(
        next(v["enum"] for v in face_variants if "enum" in v)
    )
    say_variants = schema["properties"]["say"]["anyOf"]
    assert any(v.get("type") == "string" for v in say_variants)
    assert "required" not in schema  # every ForwardTool field (including face/say) has a default

    turn_schema = TurnTool.parameters_schema()
    assert turn_schema["required"] == ["direction"]  # unchanged by the mixin
    assert "face" in turn_schema["properties"]
    assert "say" in turn_schema["properties"]


def test_expressive_default_none_behaves_exactly_like_before_the_mixin() -> None:
    """With face=None, say=None (the default), the mixin must not change a
    single thing about the existing set -> run(seconds) -> stop dispatch."""
    robot = FakeRobot()
    result = ForwardTool().execute(cast(PalmimoLike, robot))
    assert robot.calls == [("forward",), ("run", 1.5), ("stop",), ("run", _STANCE_SETTLE_SECONDS)]
    assert result.text == "moved forward for 1.5s"  # no face/say note appended


def test_expressive_applies_face_before_the_action() -> None:
    display = FakeDisplay()
    robot = FakeRobot(display=display)
    result = WaveTool(face="HAPPY").execute(cast(PalmimoLike, robot))
    assert display.calls == [("set_expression", "HAPPY", 0)]
    # set_expression is called (via robot.set_expression) before the motion starts.
    assert robot.calls[0] == ("set_expression", "HAPPY", 0)
    assert robot.calls[1] == ("wave",)
    assert "HAPPY" not in result.text or "not shown" not in result.text  # no failure note


def test_expressive_face_failure_is_noted_but_not_fatal() -> None:
    """No display attached: set_expression() no-ops (robot.display is None), so
    the face is noted as not-shown, but the action still runs and the result
    is not is_error."""
    robot = FakeRobot()  # no display
    result = WaveTool(face="HAPPY").execute(cast(PalmimoLike, robot))
    assert robot.calls[0] == ("wave",)  # the action still ran
    assert "not shown" in result.text
    assert result.is_error is False


def test_expressive_display_error_reply_is_noted() -> None:
    display = FakeDisplay(reply="ERR NOT_A_FACE (try LIST)")
    robot = FakeRobot(display=display)
    result = WaveTool(face="HAPPY").execute(cast(PalmimoLike, robot))
    assert "not shown" in result.text
    assert "ERR NOT_A_FACE" in result.text
    assert result.is_error is False  # a say/face note never makes the whole call is_error


def test_expressive_say_starts_before_action_and_is_joined_after() -> None:
    speaker = FakeSpeaker()
    robot = FakeRobot(speaker=speaker)
    result = ForwardTool(say="hello!").execute(cast(PalmimoLike, robot))
    assert robot.calls[0] == ("say", "hello!")  # started before the action
    assert robot.calls[1] == ("forward",)
    assert "said: 'hello!'" in result.text


def test_expressive_say_no_speaker_is_noted_but_not_fatal() -> None:
    robot = FakeRobot()  # no speaker: robot.say() itself no-ops (returns None)
    result = ForwardTool(say="hello!").execute(cast(PalmimoLike, robot))
    assert ("forward",) in robot.calls  # the action still ran
    assert "no speaker attached" in result.text
    assert result.is_error is False


def test_expressive_say_handle_error_is_noted_but_not_fatal() -> None:
    handle = FakeSpeechHandle(alive=False, error=RuntimeError("piper crashed"))
    speaker = FakeSpeaker(handle=handle)
    robot = FakeRobot(speaker=speaker)
    result = ForwardTool(say="hello!").execute(cast(PalmimoLike, robot))
    assert "speech failed" in result.text
    assert "piper crashed" in result.text
    assert result.is_error is False


def test_expressive_still_speaking_after_join_timeout_is_noted() -> None:
    handle = FakeSpeechHandle(alive=True)
    speaker = FakeSpeaker(handle=handle)
    robot = FakeRobot(speaker=speaker)
    result = ForwardTool(say="hello!").execute(cast(PalmimoLike, robot))
    assert "still speaking" in result.text
    assert result.is_error is False


def test_expressive_say_is_truncated_to_60_chars() -> None:
    tool = ForwardTool(say="x" * 200)
    assert tool.say is not None
    assert len(tool.say) == 60


def test_expressive_say_rejects_empty_after_strip() -> None:
    with pytest.raises(ValidationError):
        ForwardTool(say="   ")


def test_expressive_face_and_say_both_noted_together() -> None:
    display = FakeDisplay()
    speaker = FakeSpeaker()
    robot = FakeRobot(display=display, speaker=speaker)
    result = WaveTool(face="HAPPY", say="hi!").execute(cast(PalmimoLike, robot))
    assert "said: 'hi!'" in result.text
    assert robot.calls[0] == ("set_expression", "HAPPY", 0)
    assert robot.calls[1] == ("say", "hi!")
    assert robot.calls[2] == ("wave",)


def test_expressive_joins_speech_handle_even_if_act_raises() -> None:
    """Expressive.execute()'s _finish_say() runs even when _act() raises
    (e.g. MotionCancelled from a cancelled run()) -- otherwise a background
    SpeechHandle started before the action would leak un-joined. The
    exception itself still propagates unchanged; only the note text is
    skipped on this path, since there is no successful result to attach it
    to."""
    handle = FakeSpeechHandle(alive=False)
    speaker = FakeSpeaker(handle=handle)
    boom = MotionCancelled("cancelled")
    robot = FakeRobot(speaker=speaker, run_raises=boom)
    with pytest.raises(MotionCancelled):
        ForwardTool(say="hello!").execute(cast(PalmimoLike, robot))
    assert handle.join_calls  # _finish_say() ran despite the exception


def test_expressive_is_a_tool_subclass_with_abstract_act() -> None:
    assert issubclass(Expressive, Tool)
    with pytest.raises(NotImplementedError):
        Expressive()._act(cast(PalmimoLike, FakeRobot()))


# ----------------------------------------------------------------------
# Dispatch: movement / gesture tools follow set -> run(seconds) -> stop.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_cls, kwargs, expected_start",
    [
        (ForwardTool, {}, "forward"),
        (BackwardTool, {}, "backward"),
        (CreepTool, {}, "creep"),
        (DanceTool, {}, "dance"),
        (BodyTiltTool, {}, "body_tilt"),
        (PushupTool, {}, "pushup"),
        (WaveBothTool, {}, "wave_both"),
        (ClapTool, {}, "clap"),
        (BowTool, {}, "bow"),
        (StretchTool, {}, "stretch"),
        (NodTool, {}, "nod"),
        (HeadShakeTool, {}, "head_shake"),
        (WaveTool, {}, "wave"),
    ],
)
def test_simple_motion_tools_follow_set_run_stop_settle(
    tool_cls: type[Tool], kwargs: dict[str, Any], expected_start: str
) -> None:
    """set_motion -> run(seconds) -> stop -> run(_STANCE_SETTLE_SECONDS): a normal
    timed-motion return also settles the legs to stance.
    """
    robot = FakeRobot()
    tool = tool_cls(**kwargs)
    result = tool.execute(cast(PalmimoLike, robot))
    assert robot.calls == [
        (expected_start,),
        ("run", getattr(tool, "seconds")),  # noqa: B009 -- tool_cls: type[Tool] has no static .seconds
        ("stop",),
        ("run", _STANCE_SETTLE_SECONDS),
    ]
    assert isinstance(result, ToolResult)
    assert str(getattr(tool, "seconds")) in result.text  # noqa: B009 -- tool_cls: type[Tool] has no static .seconds


@pytest.mark.parametrize(
    "tool_cls, kwargs, expected_start",
    [
        (ForwardTool, {}, "forward"),
        (DanceTool, {}, "dance"),
        (WaveTool, {}, "wave"),
    ],
)
def test_timed_motion_tools_stop_even_if_run_raises(
    tool_cls: type[Tool], kwargs: dict[str, Any], expected_start: str
) -> None:
    """run() raising mid-motion must not skip stop().

    The stance-settle run() must NOT fire on this path: it runs after the
    try/finally, which the exception short-circuits, so a driver bus that
    just failed a write is never pushed with another command.
    """
    boom = RuntimeError("driver write failed")
    robot = FakeRobot(run_raises=boom)
    tool = tool_cls(**kwargs)
    with pytest.raises(RuntimeError, match="driver write failed"):
        tool.execute(cast(PalmimoLike, robot))
    assert robot.calls == [
        (expected_start,),
        ("run", getattr(tool, "seconds")),  # noqa: B009 -- tool_cls: type[Tool] has no static .seconds
        ("stop",),
    ]


def test_movement_tool_stops_when_a_real_run_is_cancelled_mid_motion() -> None:
    """MotionCancelled from a REAL Palmimo.run()/cancel() still reaches
    _run_motion's try/finally stop() -- the same guarantee
    test_timed_motion_tools_stop_even_if_run_raises exercises with a fake
    RuntimeError, but end-to-end through the real cancel()/MotionCancelled
    wiring (Palmimo.cancel), not the FakeRobot double."""

    def on_step(_pos: dict[str, int]) -> None:
        robot.cancel()

    robot = Palmimo(fps=200, on_step=on_step)  # cancels itself on the first frame
    tool = ForwardTool(seconds=5.0)
    with pytest.raises(MotionCancelled):
        tool.execute(cast(PalmimoLike, robot))
    assert robot.motion == "idle"  # stop() ran (in the finally) despite the cancellation


def test_forward_default_seconds_is_1_5() -> None:
    robot = FakeRobot()
    ForwardTool().execute(cast(PalmimoLike, robot))
    assert robot.calls == [("forward",), ("run", 1.5), ("stop",), ("run", _STANCE_SETTLE_SECONDS)]


@pytest.mark.parametrize(
    "direction, expected_call",
    [("left", "rotate_left"), ("right", "rotate_right")],
)
def test_turn_dispatches_to_rotate_left_or_right(direction: Literal["left", "right"], expected_call: str) -> None:
    robot = FakeRobot()
    TurnTool(direction=direction, seconds=2.0).execute(cast(PalmimoLike, robot))
    assert robot.calls == [(expected_call,), ("run", 2.0), ("stop",), ("run", _STANCE_SETTLE_SECONDS)]


@pytest.mark.parametrize(
    "direction, expected_call",
    [("left", "strafe_left"), ("right", "strafe_right")],
)
def test_strafe_dispatches_to_strafe_left_or_right(direction: Literal["left", "right"], expected_call: str) -> None:
    robot = FakeRobot()
    StrafeTool(direction=direction, seconds=2.0).execute(cast(PalmimoLike, robot))
    assert robot.calls == [(expected_call,), ("run", 2.0), ("stop",), ("run", _STANCE_SETTLE_SECONDS)]


# ----------------------------------------------------------------------
# Neck: look() / look_center() set the target then run() briefly to
# actually stream the interpolated frames to the driver -- Palmimo.look()
# only sets an internal target; nothing moves until step()/run() drives it.
# ----------------------------------------------------------------------


def test_look_wraps_pitch_and_yaw_as_axis_specific_degrees_and_settles_the_neck() -> None:
    """LookTool passes raw NeckPitchDegrees/NeckYawDegrees to robot.look(); the
    deg->normalized division now happens inside Palmimo.look() itself against
    the engine's real per-axis neck travel, not a hand-picked per-axis max --
    the old hand-picked YAW_MAX_DEG=60 was more than double the neck's actual
    ~26 deg travel, so LLM-requested yaws silently moved less than half as
    far as asked.
    """
    robot = FakeRobot()
    LookTool(pitch=15.0, yaw=-20.0).execute(cast(PalmimoLike, robot))
    assert robot.calls == [("look", NeckPitchDegrees(15.0), NeckYawDegrees(-20.0)), ("run", 0.6)]


def test_look_default_is_center() -> None:
    robot = FakeRobot()
    LookTool().execute(cast(PalmimoLike, robot))
    assert robot.calls == [("look", NeckPitchDegrees(0.0), NeckYawDegrees(0.0)), ("run", 0.6)]


def test_look_center_calls_facade_look_center_then_settles() -> None:
    robot = FakeRobot()
    result = LookCenterTool().execute(cast(PalmimoLike, robot))
    assert robot.calls == [("look_center",), ("run", 0.6)]
    assert "center" in result.text


# ----------------------------------------------------------------------
# Validation errors (seconds out of range, missing required field).
# ----------------------------------------------------------------------


def test_forward_seconds_too_large_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        ForwardTool(seconds=999.0)


def test_forward_seconds_too_small_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        ForwardTool(seconds=0.1)


def test_dance_seconds_upper_bound_is_15_not_10() -> None:
    ForwardTool(seconds=10.0)  # movement cap: ok at the boundary
    with pytest.raises(ValidationError):
        ForwardTool(seconds=15.0)  # would be fine for a gesture tool, not a movement one
    DanceTool(seconds=15.0)  # gesture cap: 15 is fine
    with pytest.raises(ValidationError):
        DanceTool(seconds=15.1)


def test_turn_missing_direction_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        TurnTool(seconds=1.0)  # type: ignore[call-arg]  # deliberately missing the required field


def test_look_pitch_out_of_range_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        LookTool(pitch=MotionEngine.NECK_PITCH_TRAVEL_DEG + 1.0, yaw=0.0)


def test_look_yaw_out_of_range_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        LookTool(pitch=0.0, yaw=MotionEngine.NECK_YAW_TRAVEL_DEG + 1.0)


def test_look_schema_range_matches_engine_neck_travel_for_both_axes() -> None:
    """pitch/yaw declared ranges now equal the engine's real PER-AXIS neck
    travel -- previously pitch was capped at a hand-picked 30 deg and yaw at
    60 deg, more than double the neck's actual ~26.4 deg mechanical travel,
    so the schema silently overpromised range an LLM could never actually
    reach.
    """
    schema = LookTool.parameters_schema()
    pitch_travel = MotionEngine.NECK_PITCH_TRAVEL_DEG
    yaw_travel = MotionEngine.NECK_YAW_TRAVEL_DEG
    assert schema["properties"]["pitch"]["minimum"] == -pitch_travel
    assert schema["properties"]["pitch"]["maximum"] == pitch_travel
    assert schema["properties"]["yaw"]["minimum"] == -yaw_travel
    assert schema["properties"]["yaw"]["maximum"] == yaw_travel


def test_extra_argument_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ForwardTool(seconds=1.0, bogus_field=True)  # type: ignore[call-arg]  # deliberately an unknown field


def test_say_text_over_max_length_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        SayTool(text="x" * 501)


def test_say_text_at_max_length_is_accepted() -> None:
    SayTool(text="x" * 500)


# ----------------------------------------------------------------------
# set_face / say / capture: peripheral-present and peripheral-absent paths.
# ----------------------------------------------------------------------


def test_set_face_delegates_to_robot_set_expression_facade() -> None:
    """Goes through robot.set_expression(), not robot.display.set_expression() directly."""
    display = FakeDisplay()
    robot = FakeRobot(display=display)
    result = SetFaceTool(name="happy").execute(cast(PalmimoLike, robot))
    assert robot.calls == [("set_expression", "happy", 0)]
    assert display.calls == [("set_expression", "happy", 0)]
    assert "happy" in result.text


def test_set_face_without_display_reports_observation_not_error() -> None:
    robot = FakeRobot(display=None)
    result = SetFaceTool(name="happy").execute(cast(PalmimoLike, robot))
    assert isinstance(result, ToolResult)
    assert "no face display" in result.text
    assert robot.calls == []  # short-circuits before calling the facade


def test_show_emoji_delegates_to_facade_with_hold_ms() -> None:
    """Same EXPR contract as set_face, but with a nonzero hold_ms for auto-revert."""
    display = FakeDisplay()
    robot = FakeRobot(display=display)
    result = ShowEmojiTool(name="MARU", seconds=2.0).execute(cast(PalmimoLike, robot))
    assert robot.calls == [("set_expression", "MARU", 2000)]
    assert "MARU" in result.text


def test_show_emoji_schema_exposes_name_and_optional_seconds() -> None:
    schema = ShowEmojiTool.parameters_schema()
    assert "name" in schema["properties"]
    assert "name_" not in schema["properties"]
    assert schema["required"] == ["name"]
    assert "seconds" in schema["properties"]


def test_show_emoji_without_display_reports_observation_not_error() -> None:
    robot = FakeRobot(display=None)
    result = ShowEmojiTool(name="STAR", seconds=3.0).execute(cast(PalmimoLike, robot))
    assert isinstance(result, ToolResult)
    assert "no face display" in result.text
    assert robot.calls == []


def test_set_face_reports_failure_when_display_replies_err() -> None:
    """An `ERR ... (try LIST)` firmware reply must not read as a success."""
    display = FakeDisplay(reply="ERR NOT_A_FACE (try LIST)")
    robot = FakeRobot(display=display)
    result = SetFaceTool(name="NOT_A_FACE").execute(cast(PalmimoLike, robot))
    assert result.is_error is True
    assert "failed" in result.text
    assert "NOT_A_FACE" in result.text
    assert "ERR NOT_A_FACE (try LIST)" in result.text
    # Must NOT read as a success to an LLM scanning for "showing"/"OK".
    assert "showing expression" not in result.text


def test_show_emoji_reports_failure_when_display_replies_err() -> None:
    """Same ERR-reply contract as set_face, for show_emoji."""
    display = FakeDisplay(reply="ERR NOT_A_SYMBOL (try LIST)")
    robot = FakeRobot(display=display)
    result = ShowEmojiTool(name="NOT_A_SYMBOL", seconds=3.0).execute(cast(PalmimoLike, robot))
    assert result.is_error is True
    assert "failed" in result.text
    assert "NOT_A_SYMBOL" in result.text
    assert "ERR NOT_A_SYMBOL (try LIST)" in result.text
    assert "showing emoji" not in result.text


@pytest.mark.parametrize(
    "reply",
    [
        " ERR NOT_A_FACE (try LIST) ",  # incidental whitespace around the ERR token
        "err NOT_A_FACE (try LIST)",  # incidental lowercasing
    ],
)
def test_set_face_reports_failure_for_err_reply_regardless_of_whitespace_or_case(reply: str) -> None:
    display = FakeDisplay(reply=reply)
    robot = FakeRobot(display=display)
    result = SetFaceTool(name="NOT_A_FACE").execute(cast(PalmimoLike, robot))
    assert result.is_error is True
    assert "failed" in result.text


@pytest.mark.parametrize("reply", ["OK ERRATA", "ERRORISH THING", "ERROR OUT OF RANGE"])
def test_set_face_does_not_misdetect_err_like_replies_as_failures(reply: str) -> None:
    """`startswith("ERR")` would false-positive on ERROR/ERRATA; the token match must not."""
    display = FakeDisplay(reply=reply)
    robot = FakeRobot(display=display)
    result = SetFaceTool(name="happy").execute(cast(PalmimoLike, robot))
    assert result.is_error is False
    assert "showing expression" in result.text


@pytest.mark.parametrize("reply", ["", "   "])
def test_set_face_reports_failure_when_display_reply_is_empty(reply: str) -> None:
    """An empty/whitespace-only reply means FaceDisplay._command() hit its serial
    timeout (see io/display.py's `_command`, which returns "" when no reply
    arrives) -- it must not be read as a silent success."""
    display = FakeDisplay(reply=reply)
    robot = FakeRobot(display=display)
    result = SetFaceTool(name="happy").execute(cast(PalmimoLike, robot))
    assert result.is_error is True
    assert "no reply" in result.text
    assert "showing expression" not in result.text


@pytest.mark.parametrize("reply", ["", "   "])
def test_show_emoji_reports_failure_when_display_reply_is_empty(reply: str) -> None:
    display = FakeDisplay(reply=reply)
    robot = FakeRobot(display=display)
    result = ShowEmojiTool(name="STAR", seconds=3.0).execute(cast(PalmimoLike, robot))
    assert result.is_error is True
    assert "no reply" in result.text
    assert "showing emoji" not in result.text


def test_show_emoji_seconds_out_of_range_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        ShowEmojiTool(name="STAR", seconds=99.0)


def test_say_delegates_to_robot_say_and_joins_briefly() -> None:
    handle = FakeSpeechHandle(alive=False, error=None)
    speaker = FakeSpeaker(handle=handle)
    robot = FakeRobot(speaker=speaker)
    result = SayTool(text="hello there").execute(cast(PalmimoLike, robot))
    assert robot.calls == [("say", "hello there")]
    assert speaker.calls == [("say", "hello there", None)]
    assert handle.join_calls == [0.5]
    assert "hello there" in result.text
    assert result.text.startswith("spoke:")


def test_say_reports_speech_failed_when_handle_errors_within_timeout() -> None:
    handle = FakeSpeechHandle(alive=False, error=RuntimeError("piper not found"))
    speaker = FakeSpeaker(handle=handle)
    robot = FakeRobot(speaker=speaker)
    result = SayTool(text="hello").execute(cast(PalmimoLike, robot))
    assert "speech failed" in result.text
    assert "piper not found" in result.text


def test_say_reports_background_when_still_speaking_after_timeout() -> None:
    handle = FakeSpeechHandle(alive=True)
    speaker = FakeSpeaker(handle=handle)
    robot = FakeRobot(speaker=speaker)
    result = SayTool(text="a long sentence").execute(cast(PalmimoLike, robot))
    assert "speaking (in background)" in result.text
    assert "a long sentence" in result.text


def test_say_without_speaker_reports_observation_not_error() -> None:
    robot = FakeRobot(speaker=None)
    result = SayTool(text="hello").execute(cast(PalmimoLike, robot))
    assert isinstance(result, ToolResult)
    assert "no speaker" in result.text


def test_capture_without_camera_reports_observation_not_error() -> None:
    robot = FakeRobot(camera=None)
    result = CaptureTool().execute(cast(PalmimoLike, robot))
    assert isinstance(result, ToolResult)
    assert "no camera" in result.text
    assert result.images == []


def test_capture_when_camera_read_fails_reports_observation() -> None:
    camera = FakeCamera(ok=False, frame=None)
    robot = FakeRobot(camera=camera)
    result = CaptureTool().execute(cast(PalmimoLike, robot))
    assert camera.calls == ["read"]
    assert "failed" in result.text
    assert result.images == []


@pytest.mark.skipif(not HAS_CV2, reason="opencv (cv2) not installed")
def test_capture_encodes_frame_as_jpeg() -> None:
    import numpy as np

    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    camera = FakeCamera(ok=True, frame=frame)
    robot = FakeRobot(camera=camera)
    result = CaptureTool().execute(cast(PalmimoLike, robot))
    assert result.text == "captured image from head camera"
    assert len(result.images) == 1
    jpeg = result.images[0]
    assert isinstance(jpeg, bytes)
    assert jpeg[:2] == b"\xff\xd8"  # JPEG SOI marker


# ----------------------------------------------------------------------
# stop
# ----------------------------------------------------------------------


def test_stop_stops_motion_recenters_gaze_and_settles_the_neck() -> None:
    robot = FakeRobot()
    result = StopTool().execute(cast(PalmimoLike, robot))
    assert robot.calls == [("stop",), ("look_center",), ("run", 0.6)]
    assert isinstance(result, ToolResult)


# ----------------------------------------------------------------------
# sleep / wake -- thin wrappers over Palmimo.sleep()/wake().
# ----------------------------------------------------------------------


def test_sleep_tool_calls_robot_sleep() -> None:
    robot = FakeRobot()
    result = SleepTool().execute(cast(PalmimoLike, robot))
    assert robot.calls == [("sleep",)]
    assert result.text == "went to sleep"


def test_wake_tool_calls_robot_wake() -> None:
    robot = FakeRobot()
    result = WakeTool().execute(cast(PalmimoLike, robot))
    assert robot.calls == [("wake",)]
    assert result.text == "woke up"


# ----------------------------------------------------------------------
# Integration smoke: real (compute-only) Palmimo accepts the immediate
# neck tools without a background stepping loop. Timed tools that call
# run() are covered above via FakeRobot to avoid sleeping in tests.
# ----------------------------------------------------------------------


def test_look_and_stop_against_real_compute_only_palmimo() -> None:
    robot = Palmimo()
    look_result = LookTool(pitch=10.0, yaw=-20.0).execute(cast(PalmimoLike, robot))
    stop_result = StopTool().execute(cast(PalmimoLike, robot))
    assert isinstance(look_result, ToolResult)
    assert isinstance(stop_result, ToolResult)
    assert robot.motion == "idle"


def test_sleep_and_wake_against_real_compute_only_palmimo() -> None:
    """No driver attached -- Palmimo.sleep()/wake() no-op (see their own
    "no driver / not connected" early return), so this is dry-run safe."""
    robot = Palmimo()
    sleep_result = SleepTool().execute(cast(PalmimoLike, robot))
    wake_result = WakeTool().execute(cast(PalmimoLike, robot))
    assert isinstance(sleep_result, ToolResult)
    assert isinstance(wake_result, ToolResult)


# ----------------------------------------------------------------------
# Dependency-free regression: `import palmimo_sdk` itself must not pull in
# pydantic -- only palmimo_sdk.agent (this subpackage) needs it. See the
# `agent` extra's rationale in pyproject.toml / palmimo_sdk/agent/__init__.py.
# ----------------------------------------------------------------------


def test_import_palmimo_sdk_is_pydantic_free() -> None:
    """``import palmimo_sdk`` must not pull in pydantic (agent lazy-import regression guard).

    Checked in a separate process so the verdict isn't polluted by import
    state left behind by other tests in the suite (the agent tests do use
    pydantic).
    """
    code = (
        "import sys, palmimo_sdk; assert 'pydantic' not in sys.modules, "
        "sorted(m for m in sys.modules if 'pydantic' in m)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_run_motion_settles_stance_after_motion_cancelled() -> None:
    """A cancelled motion still glides the legs onto a stance pose before the
    MotionCancelled propagates -- verified on hardware that skipping this
    leaves one tripod frozen mid-stride, bent and loaded."""
    calls = {"n": 0}

    def on_step(_pos: dict[str, int]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            robot.cancel()

    robot = Palmimo(fps=1000, on_step=on_step)
    with pytest.raises(MotionCancelled):
        _run_motion(robot, robot.forward, seconds=5.0)

    assert robot.motion == "idle"
    # The post-cancel settle actually streamed: positions eased to stance
    # (2048 everywhere except the neck pitch trim) instead of freezing on
    # the first mid-stride frame the cancel landed on.
    center = robot._engine.neck_pitch_center()
    for name, tick in robot.positions.items():
        expected = center if name == "neck_pitch1" else 2048
        assert abs(tick - expected) <= 2, (name, tick, expected)
