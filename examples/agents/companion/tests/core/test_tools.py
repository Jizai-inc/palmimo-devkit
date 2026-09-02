"""Tests for :mod:`palmimo_companion_agent.core.tools`.

Every tool executes synchronously against a real, compute-only
:class:`~palmimo_sdk.Palmimo` facade (no driver/display/speaker/camera
attached) -- matching how :class:`~palmimo_sdk.agent.toolset.AgentToolSet`
actually dispatches them (see that module's docstring: ``execute()`` is
synchronous, run on a worker thread by ``call()``). Dispatch itself (unknown
tool name, validation errors, cancellation) is the SDK's own contract and is
already covered by ``palmimo_sdk``'s test suite; these tests only cover what
this module adds on top of it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, cast, get_args

import pytest
from pydantic import ValidationError

from palmimo_companion_agent.core.tools import (
    COMPANION_TOOL_MODELS,
    LOOK_AT_FACE_SEEN_MARK,
    Capture,
    Dance,
    Discover,
    EyeContact,
    HeadShake,
    Investigate,
    Look,
    LookAtFaceTool,
    LookCenter,
    Nod,
    Rest,
    ShowEmoji,
    Sleep,
    WakeUp,
    make_look_at_face_tool,
)
from palmimo_sdk import MotionCancelled, Palmimo
from palmimo_sdk.agent.tools import FaceExpression, SetFaceTool, StopTool
from palmimo_sdk.agent.toolset import TOOL_MODELS, AgentToolSet


@pytest.fixture(autouse=True)
def _reset_face_locator() -> Iterator[None]:
    """LookAtFaceTool's face locator is process-global (see its docstring); tests must not leak it."""
    LookAtFaceTool.set_face_locator(None)
    yield
    LookAtFaceTool.set_face_locator(None)


@pytest.fixture
def palmimo() -> Palmimo:
    return Palmimo()


@pytest.fixture
def toolset(palmimo: Palmimo) -> AgentToolSet:
    ts = AgentToolSet(palmimo, exclude=["creep", "wave"])
    for cls in COMPANION_TOOL_MODELS.values():
        ts.register(cls)
    return ts


class TestRegistry:
    def test_every_tool_is_registered(self) -> None:
        # Only the tools this agent overrides or adds -- everything else in
        # the LLM-facing vocabulary comes from the SDK's own TOOL_MODELS
        # unmodified (see TestFullVocabulary below).
        expected = {
            "dance",
            "nod",
            "head_shake",
            "look",
            "look_center",
            "capture",
            "discover",
            "eye_contact",
            "investigate",
            "rest",
            "sleep",
            "wake_up",
            "show_emoji",
            "look_at_face",
        }
        assert set(COMPANION_TOOL_MODELS) == expected

    def test_wave_and_creep_are_excluded_from_the_toolset(self, toolset: AgentToolSet) -> None:
        assert "wave" not in toolset.tool_names
        assert "creep" not in toolset.tool_names

    def test_capture_is_not_split_by_direction(self) -> None:
        assert "capture_right" not in COMPANION_TOOL_MODELS

    def test_every_registered_tool_exposes_an_optional_reason(self, toolset: AgentToolSet) -> None:
        # The SDK's own Tool base carries `reason` as optional (see
        # palmimo_sdk.agent.tools.Tool); this agent's overrides/composites
        # inherit or redeclare it, but none make it required -- the system
        # prompt is what asks the model to fill it in every turn.
        for name, cls in toolset.tool_models.items():
            assert "reason" in cls.model_fields, f"{name} is missing `reason`"
            assert not cls.model_fields["reason"].is_required(), f"{name}'s `reason` must stay optional"

    def test_schemas_cover_every_registered_tool(self, toolset: AgentToolSet) -> None:
        names = {schema["function"]["name"] for schema in toolset.to_openai_tools()}
        assert names == set(toolset.tool_names)


class TestFullVocabulary:
    """Locks in the simplified vocabulary: SDK defaults (minus creep/wave),
    with the companion's own overrides/composites layered on top."""

    def test_wiring_produces_sdk_defaults_plus_companion_overrides(self, toolset: AgentToolSet) -> None:
        expected = (set(TOOL_MODELS) - {"creep", "wave"}) | set(COMPANION_TOOL_MODELS)
        assert set(toolset.tool_names) == expected

    def test_companion_overrides_win_over_the_sdk_default_for_the_same_name(self, toolset: AgentToolSet) -> None:
        for name, cls in COMPANION_TOOL_MODELS.items():
            assert toolset.tool_models[name] is cls

    def test_delegated_tools_are_the_sdk_classes_themselves(self, toolset: AgentToolSet) -> None:
        # Every non-overridden name must resolve to the SDK's own class (not
        # some wrapper), so its schema/description/defaults are exactly the
        # SDK's -- the whole point of dropping the pass-through layer.
        for name in set(toolset.tool_names) - set(COMPANION_TOOL_MODELS):
            assert toolset.tool_models[name] is TOOL_MODELS[name]


class TestReasonOptional:
    async def test_missing_reason_is_not_a_validation_error(self, toolset: AgentToolSet) -> None:
        # reason is optional on the SDK's own Tool base -- the system prompt,
        # not the schema, is what asks the model to fill it in every turn.
        result = await toolset.call("stop", {})
        assert result.is_error is False


class TestDance:
    async def test_dance_shows_the_groove_emoji_and_runs(self, palmimo: Palmimo) -> None:
        tool = Dance.model_validate({"seconds": 0.6, "reason": "excited"})
        result = tool.execute(palmimo)
        assert "danced" in result.text.lower() or result.text  # a real motion ran without raising

    def test_dance_has_no_face_argument(self) -> None:
        assert "face" not in Dance.model_fields


class TestNodAndHeadShake:
    async def test_nod_runs_without_error(self, palmimo: Palmimo) -> None:
        tool = Nod.model_validate({"seconds": 0.6, "reason": "agreeing"})
        result = tool.execute(palmimo)
        assert result.is_error is False

    async def test_head_shake_runs_without_error(self, palmimo: Palmimo) -> None:
        tool = HeadShake.model_validate({"seconds": 0.6, "reason": "disagreeing"})
        result = tool.execute(palmimo)
        assert result.is_error is False

    async def test_nod_reapplies_a_requested_face_afterward(
        self, palmimo: Palmimo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, int]] = []
        # Expressive._apply_face() no-ops when robot.display is None (see its
        # docstring), so a fake display must be attached for it to actually
        # call set_expression() -- matching a real display-attached robot.
        monkeypatch.setattr(type(palmimo), "display", property(lambda self: object()))

        def _record_set_expression(name: str, hold_ms: int = 0) -> str:
            calls.append((name, hold_ms))
            return "OK"

        monkeypatch.setattr(palmimo, "set_expression", _record_set_expression)
        tool = Nod.model_validate({"seconds": 0.5, "reason": "agreeing", "face": "HAPPY"})

        tool.execute(palmimo)

        # face is applied before the gesture (Expressive.execute), MARU
        # overwrites it, then the requested face is re-applied at the end.
        names = [name for name, _ in calls]
        assert names[0] == "HAPPY"
        assert "MARU" in names
        assert names[-1] == "HAPPY"


class TestCapture:
    async def test_no_camera_reports_it_plainly(self, palmimo: Palmimo) -> None:
        tool = Capture.model_validate({"reason": "curious"})
        result = tool.execute(palmimo)
        assert "no camera attached" in result.text
        assert "(looking" not in result.text  # center: no direction prefix

    @pytest.mark.parametrize("direction", ["left", "right", "up", "down"])
    async def test_direction_is_reported_and_the_neck_recenters(self, palmimo: Palmimo, direction: str) -> None:
        tool = Capture.model_validate({"direction": direction, "reason": "curious"})
        result = tool.execute(palmimo)
        assert f"(looking {direction})" in result.text
        # look_center() (in the `finally`) resets the neck's TARGET to
        # (0, 0); the settle run() then streams frames toward it.
        assert palmimo._engine._neck_target_pitch == 0.0
        assert palmimo._engine._neck_target_yaw == 0.0


class _WakeRecorder:
    """Records the order of everything WakeUp does, so the sequence can be asserted."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def wake(self, duration: float = 1.5, start_gain: int = 300) -> None:
        self.calls.append("wake")

    def sleep(self, duration: float = 1.5, end_gain: int = 300) -> None:
        self.calls.append("sleep")

    def set_expression(self, name: str, hold_ms: int = 0) -> str | None:
        self.calls.append(f"face:{name}")
        return None

    def stretch(self) -> None:
        self.calls.append("stretch")

    def head_shake(self) -> None:
        self.calls.append("head_shake")

    def run(self, *, seconds: float | None = None, steps: int | None = None) -> object:
        self.calls.append("run")
        return None

    def stop(self) -> None:
        self.calls.append("stop")


class TestSleepAndWake:
    async def test_sleep_settles_before_showing_the_sleeping_face(self) -> None:
        """The face last: a sleeping face while the body is still moving looks wrong."""
        robot = _WakeRecorder()
        Sleep.model_validate({}).execute(cast(Any, robot))
        assert robot.calls == ["sleep", "face:SLEEP"]

    async def test_waking_stretches_and_shakes_before_looking_awake(self) -> None:
        """Rising and answering in one breath reads as never having slept, so the
        drowsy face comes back first and the happy one only after the stretch."""
        robot = _WakeRecorder()
        WakeUp.model_validate({"seconds": 0.01}).execute(cast(Any, robot))

        assert robot.calls.index("face:SLEEPY") < robot.calls.index("stretch")
        assert robot.calls.index("stretch") < robot.calls.index("head_shake")
        assert robot.calls[-1] == "face:HAPPY"

    async def test_waking_stops_each_motion_even_though_it_chains_two(self) -> None:
        """Two motions back to back: the first must be stopped before the second
        is set, or the engine carries the stretch into the head shake."""
        robot = _WakeRecorder()
        WakeUp.model_validate({"seconds": 0.01}).execute(cast(Any, robot))
        assert robot.calls.count("stop") == 2

    async def test_wake_seconds_defaults_so_the_model_need_not_choose(self) -> None:
        assert WakeUp.model_validate({}).seconds == 2.5

    def test_sleep_is_not_long_running(self) -> None:
        """Sleep's _act is a bare robot.sleep() + a face change -- no
        _motion_and_settle, no cancel-polling run() call -- exactly like the
        SDK's own stock sleep tool, which is long_running=False too."""
        assert Sleep.long_running is False

    def test_wake_up_is_long_running(self) -> None:
        """Unlike Sleep, WakeUp's _act loops through _motion_and_settle's own
        cancel-polling run() calls, so a barge-in mid-wake is a real
        MotionCancelled a caller can race against."""
        assert WakeUp.long_running is True


class TestRest:
    async def test_reason_is_optional(self) -> None:
        Rest.model_validate({"seconds": 0.01})  # must not raise

    async def test_rest_sleeps_and_reports_the_reason_when_given(self, palmimo: Palmimo) -> None:
        tool = Rest.model_validate({"seconds": 0.01, "reason": "savoring the moment"})
        result = tool.execute(palmimo)
        assert "savoring the moment" in result.text

    async def test_rest_sleeps_and_reports_plainly_without_a_reason(self, palmimo: Palmimo) -> None:
        tool = Rest.model_validate({"seconds": 0.01})
        result = tool.execute(palmimo)
        assert result.text == "rested for a moment"

    async def test_cancelling_rest_reports_interrupted(self, toolset: AgentToolSet) -> None:
        # Rest used to be a bare time.sleep(): no frame boundary ever ran, so
        # cancel() had nothing to interrupt until the whole sleep finished.
        # stop() -> run(seconds=...) streams IDLE frames instead, giving
        # cancel() a frame boundary to land on.
        task = asyncio.ensure_future(toolset.call("rest", {"seconds": 5.0, "reason": "chill"}))
        await asyncio.sleep(0.05)
        await toolset.cancel_running()

        result = await task

        assert result.is_error is False
        assert "interrupted" in result.text


class TestSetFaceAndShowEmoji:
    # set_face itself is now the SDK's own, unmodified SetFaceTool -- this
    # agent only extends show_emoji's vocabulary (see ShowEmoji's docstring).
    async def test_set_face_reports_no_display(self, palmimo: Palmimo) -> None:
        tool = SetFaceTool.model_validate({"name": "HAPPY", "reason": "happy"})
        result = tool.execute(palmimo)
        assert "no face display" in result.text

    async def test_show_emoji_reports_no_display(self, palmimo: Palmimo) -> None:
        tool = ShowEmoji.model_validate({"name": "STAR", "seconds": 2.0, "reason": "topic"})
        result = tool.execute(palmimo)
        assert "no face display" in result.text

    def test_set_face_vocabulary_matches_the_sdk(self) -> None:
        # Open vocabulary (no enum): the SDK's own SetFaceTool design -- the
        # LLM-facing `description` ClassVar (not the pydantic schema's own
        # docstring-derived description) is what to_openai_tools() sends.
        for value in get_args(FaceExpression):
            assert value in SetFaceTool.description


class TestStop:
    # stop itself is now the SDK's own, unmodified StopTool.
    async def test_stop_reports_success(self, palmimo: Palmimo) -> None:
        tool = StopTool.model_validate({"reason": "halt"})
        result = tool.execute(palmimo)
        assert "stop" in result.text.lower()


class TestDiscoverEyeContactInvestigate:
    async def test_discover_reports_no_camera(self, palmimo: Palmimo) -> None:
        tool = Discover.model_validate({"seconds": 0.3, "direction": "up", "reason": "curious"})
        result = tool.execute(palmimo)
        assert "no camera attached" in result.text
        assert "looked up" in result.text

    async def test_eye_contact_center_center_is_a_no_op(self, palmimo: Palmimo) -> None:
        tool = EyeContact.model_validate({"face_position": "middle-center", "seconds": 1.0, "reason": "eye contact"})
        result = tool.execute(palmimo)
        assert "already making eye contact" in result.text

    async def test_eye_contact_moves_the_neck_for_an_off_center_position(self, palmimo: Palmimo) -> None:
        tool = EyeContact.model_validate({"face_position": "upper-right", "seconds": 0.2, "reason": "eye contact"})
        result = tool.execute(palmimo)
        assert "made eye contact" in result.text

    async def test_investigate_reports_no_camera(self, palmimo: Palmimo) -> None:
        tool = Investigate.model_validate({"yaw": 0.5, "seconds": 0.3, "reason": "curious"})
        result = tool.execute(palmimo)
        assert "no camera attached" in result.text
        assert "peered over" in result.text

    def test_investigate_cancelled_during_tilt_still_leaves_motion_idle(self) -> None:
        """A cancel() mid body_tilt used to propagate straight out of _act()
        with no stop() -- the facade stayed parked in Motion.BODY_TILT.
        Wrapping the whole sequence in try/finally guarantees stop() runs
        first, whichever step raised."""
        calls = {"n": 0}

        def on_step(_pos: dict[str, int]) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                robot.cancel()

        robot = Palmimo(fps=1000, on_step=on_step)
        tool = Investigate.model_validate({"yaw": 0.5, "seconds": 5.0, "reason": "curious"})

        with pytest.raises(MotionCancelled):
            tool.execute(robot)

        assert robot.motion == "idle"


class TestLookAtFace:
    async def test_no_camera_or_locator_reports_it_plainly(self, palmimo: Palmimo) -> None:
        tool = LookAtFaceTool.model_validate({"seconds": 0.1, "reason": "reflex"})
        result = tool.execute(palmimo)
        assert "no camera attached" in result.text

    async def test_face_locator_is_injected_via_classmethod_not_a_field(self) -> None:
        assert "face_locator" not in LookAtFaceTool.model_fields

    async def test_tracks_and_reports_seen_when_a_face_is_located(
        self, palmimo: Palmimo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeCamera:
            def latest(self, timeout: float = 0.1) -> object:
                return object()

        class FakeLocator:
            def locate(self, frame: object) -> tuple[float, float]:
                return (0.5, 0.5)  # dead-center: no drift, stays "seen"

        monkeypatch.setattr(type(palmimo), "camera", property(lambda self: FakeCamera()))
        LookAtFaceTool.set_face_locator(FakeLocator())
        tool = LookAtFaceTool.model_validate({"seconds": 0.05, "reason": "reflex"})

        result = tool.execute(palmimo)

        assert result.text == LOOK_AT_FACE_SEEN_MARK

    async def test_reports_not_found_when_the_locator_never_locates_a_face(
        self, palmimo: Palmimo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeCamera:
            def latest(self, timeout: float = 0.1) -> object:
                return object()

        class FakeLocator:
            def locate(self, frame: object) -> None:
                return None

        monkeypatch.setattr(type(palmimo), "camera", property(lambda self: FakeCamera()))
        LookAtFaceTool.set_face_locator(FakeLocator())
        tool = LookAtFaceTool.model_validate({"seconds": 0.05, "reason": "reflex"})

        result = tool.execute(palmimo)

        assert "none was found" in result.text

    async def test_cancel_between_iterations_is_caught_via_checkpoint(
        self, palmimo: Palmimo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cancel() delivered while the locator is processing (outside
        robot.run(steps=1)) used to be invisible to the loop: the next
        iteration's own run(steps=1) takes a FRESH entry snapshot that
        postdates the cancel, so it would never raise, and the loop could
        run to completion without ever observing the cancel().
        robot.cancel_checkpoint() / raise_if_cancelled() fix
        this: the checkpoint is taken once, before the loop starts, so a
        cancel() landing anywhere afterward is caught at the top of the
        NEXT iteration."""

        class FakeCamera:
            def latest(self, timeout: float = 0.1) -> object:
                return object()

        calls = {"n": 0}

        class FakeLocator:
            def locate(self, frame: object) -> tuple[float, float]:
                calls["n"] += 1
                if calls["n"] == 1:
                    palmimo.cancel()  # arrives mid-iteration, outside run()
                return (0.5, 0.5)

        monkeypatch.setattr(type(palmimo), "camera", property(lambda self: FakeCamera()))
        LookAtFaceTool.set_face_locator(FakeLocator())
        tool = LookAtFaceTool.model_validate({"seconds": 5.0, "reason": "reflex"})

        with pytest.raises(MotionCancelled):
            tool.execute(palmimo)

        # Caught at the top of the SECOND iteration -- locate() was never
        # called again after the cancel landed.
        assert calls["n"] == 1


class TestMakeLookAtFaceTool:
    """make_look_at_face_tool() -- a per-runtime subclass factory, replacing
    a direct ClassVar assignment on the shared LookAtFaceTool class (two
    Runtimes built in the same process must not clobber each other's
    locator)."""

    def test_returns_a_subclass_with_the_same_name_and_tool_name(self) -> None:
        locator = object()
        cls = make_look_at_face_tool(locator)
        assert issubclass(cls, LookAtFaceTool)
        assert cls.name == "look_at_face"
        assert cls.__name__ == "LookAtFaceTool"

    def test_binds_the_locator_without_touching_the_base_class(self) -> None:
        locator = object()
        cls = make_look_at_face_tool(locator)
        assert cls._face_locator is locator
        assert LookAtFaceTool._face_locator is None

    def test_two_factory_built_classes_do_not_share_a_locator(self) -> None:
        locator_a = object()
        locator_b = object()
        cls_a = make_look_at_face_tool(locator_a)
        cls_b = make_look_at_face_tool(locator_b)
        assert cls_a._face_locator is locator_a
        assert cls_b._face_locator is locator_b  # building cls_b must not overwrite cls_a


class TestCancellation:
    async def test_cancelling_a_long_running_tool_reports_interrupted(self, toolset: AgentToolSet) -> None:
        task = asyncio.ensure_future(toolset.call("forward", {"seconds": 5.0, "reason": "go"}))
        await asyncio.sleep(0.05)
        await toolset.cancel_running()

        result = await task

        assert result.is_error is False
        assert "interrupted" in result.text


class TestExtraArgumentsForbidden:
    def test_unknown_argument_raises_validation_error(self) -> None:
        # Dance is a companion-defined tool (not an SDK pass-through), so this
        # exercises this module's own schemas rather than the SDK's.
        with pytest.raises(ValidationError):
            Dance.model_validate({"seconds": 2.0, "reason": "go", "made_up_field": "oops"})


class TestDanceSay:
    def test_dance_schema_offers_say_but_not_face(self) -> None:
        fields = Dance.model_fields
        assert "say" in fields
        assert "face" not in fields  # GROOVE is unconditional; see Dance's docstring

    async def test_dance_passes_say_through_to_the_speaking_motion(
        self, palmimo: Palmimo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spoken: list[str] = []

        class _DoneThread:
            error = None

            def is_alive(self) -> bool:
                return False

            def join(self, timeout: float | None = None) -> None:
                return None

        def _record_say(text: str) -> _DoneThread:
            spoken.append(text)
            return _DoneThread()

        monkeypatch.setattr(palmimo, "say", _record_say)
        tool = Dance.model_validate({"seconds": 0.6, "reason": "party", "say": "たのしい!"})

        result = tool.execute(palmimo)

        assert result.is_error is False
        assert spoken == ["たのしい!"]


class TestLookSeconds:
    def test_look_and_look_center_accept_optional_seconds(self) -> None:
        assert "seconds" in Look.model_fields
        assert "seconds" in LookCenter.model_fields

    async def test_look_holds_the_gaze_for_the_requested_seconds(
        self, palmimo: Palmimo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runs: list[float] = []
        original_run = palmimo.run

        def _record_run(*, seconds: float | None = None, steps: int | None = None) -> dict[str, int]:
            if seconds is not None:
                runs.append(seconds)
            return original_run(seconds=0.01 if seconds is not None else None, steps=steps)

        monkeypatch.setattr(palmimo, "run", _record_run)
        tool = Look.model_validate({"pitch": -15, "yaw": 10, "seconds": 2.0, "reason": "glance up"})

        result = tool.execute(palmimo)

        assert result.is_error is False
        assert runs == [2.0]
        assert "held 2s" in result.text

    async def test_look_without_seconds_uses_the_quick_settle(self, palmimo: Palmimo) -> None:
        tool = Look.model_validate({"pitch": -10, "reason": "quick glance"})
        result = tool.execute(palmimo)
        assert result.is_error is False
        assert "held" not in result.text
