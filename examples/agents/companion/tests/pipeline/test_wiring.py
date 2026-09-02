"""Tests for :mod:`palmimo_companion_agent.pipeline.wiring`.

Every hardware peripheral is a fake monkeypatched in for the real
``palmimo_sdk`` / MediaPipe class -- no real servo bus, camera, mic, speaker,
or MediaPipe model is ever touched. Wiring itself is assembly-only (no probe,
no degrade): with ``hardware=True`` every peripheral is built unconditionally
and handed to ``Palmimo``; whether the robot actually comes up is entirely
``Runtime.start()`` / ``Runtime.connect()``'s job, delegating to the SDK's own
all-or-nothing ``Palmimo.connect()``.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Iterator

import pytest

from palmimo_companion_agent.core.reflexes import ReflexEngine
from palmimo_companion_agent.core.tools import LookAtFaceTool
from palmimo_companion_agent.core.vision import FacePresenceDetector
from palmimo_companion_agent.pipeline import wiring
from palmimo_companion_agent.pipeline.history import ToolExecEvent
from palmimo_companion_agent.pipeline.settings import PipelineSettings
from palmimo_companion_agent.pipeline.wiring import Runtime, build_runtime
from palmimo_sdk import Palmimo, SpeakerConfig


def _settings(**overrides: object) -> PipelineSettings:
    return PipelineSettings.with_env_file(None).model_copy(update=overrides)


@pytest.fixture(autouse=True)
def _api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets both API keys by default; individual tests remove one to test the preflight."""
    monkeypatch.setenv("GEMINI_API_KEY", "sk-gemini-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")


@pytest.fixture(autouse=True)
def _reset_face_locator() -> Iterator[None]:
    """LookAtFaceTool's face locator is process-global; tests must not leak it."""
    yield
    LookAtFaceTool.set_face_locator(None)


class FakeDriver:
    def __init__(self, *, port: str | None = None) -> None:
        self.port = port

    def connect(self) -> None:
        pass


class FakePeripheral:
    """Generic stand-in for FaceDisplay / Speaker / HeadCamera / MicStream."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs

    def __getattr__(self, name: str) -> object:
        def _noop(*args: object, **kwargs: object) -> None:
            return None

        return _noop


class FakeSpeechPipeline:
    """Stands in for :class:`~palmimo_companion_agent.pipeline.speech.SpeechPipeline`.

    The real class builds a Silero VAD segmenter by default (a model
    download); wiring tests never want to exercise that, so this fake
    records its constructor args instead.
    """

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.transcript_observer: object = None

    def set_transcript_observer(self, observer: object) -> None:
        self.transcript_observer = observer


class FakeVisionWatch:
    def __init__(self, camera: object, detectors: list[object]) -> None:
        self.camera = camera
        self.detectors = detectors


def _patch_all_peripherals(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Monkeypatch every real peripheral/detector class wiring constructs, returning the fake instances."""
    fakes: dict[str, object] = {
        "driver": FakeDriver(),
        "display": FakePeripheral(),
        "speaker": FakePeripheral(),
        "camera": FakePeripheral(),
        "mic": FakePeripheral(),
        "face_locator": object(),
        "wave_detector": object(),
    }
    monkeypatch.setattr(wiring, "DynamixelDriver", lambda *, port=None: fakes["driver"])
    monkeypatch.setattr(wiring, "FaceDisplay", lambda: fakes["display"])
    monkeypatch.setattr(wiring, "Speaker", lambda config, **_kwargs: fakes["speaker"])
    monkeypatch.setattr(wiring, "HeadCamera", lambda: fakes["camera"])
    monkeypatch.setattr(wiring, "MicStream", lambda **_kwargs: fakes["mic"])
    monkeypatch.setattr(wiring, "FaceLocator", lambda: fakes["face_locator"])
    monkeypatch.setattr(wiring, "WaveDetector", lambda: fakes["wave_detector"])
    monkeypatch.setattr(wiring, "SpeechPipeline", FakeSpeechPipeline)
    monkeypatch.setattr(wiring, "VisionWatch", FakeVisionWatch)
    return fakes


# ----------------------------------------------------------------------
# Preflight
# ----------------------------------------------------------------------


def test_build_runtime_raises_when_gemini_api_key_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    settings = _settings(hardware=False)

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        build_runtime(settings)


def test_build_runtime_raises_when_openai_api_key_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = _settings(hardware=False)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        build_runtime(settings)


def test_build_runtime_succeeds_when_every_configured_model_has_its_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # chat/guard/vlm default to gemini/..., stt defaults to openai/...; both are set by the autouse fixture.
    settings = _settings(hardware=False)

    rt = build_runtime(settings)

    assert isinstance(rt.palmimo, Palmimo)


# ----------------------------------------------------------------------
# hardware=False -- compute-only Palmimo, nothing else constructed
# ----------------------------------------------------------------------


def test_build_runtime_uses_a_compute_only_palmimo_when_hardware_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    def _should_not_be_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("no peripheral should be constructed when settings.hardware is False")

    monkeypatch.setattr(wiring, "DynamixelDriver", _should_not_be_called)
    monkeypatch.setattr(wiring, "FaceDisplay", _should_not_be_called)
    monkeypatch.setattr(wiring, "Speaker", _should_not_be_called)
    monkeypatch.setattr(wiring, "HeadCamera", _should_not_be_called)
    monkeypatch.setattr(wiring, "MicStream", _should_not_be_called)
    monkeypatch.setattr(wiring, "FaceLocator", _should_not_be_called)
    monkeypatch.setattr(wiring, "WaveDetector", _should_not_be_called)

    rt = build_runtime(_settings(hardware=False))

    assert isinstance(rt.palmimo, Palmimo)
    assert rt.palmimo.driver is None
    assert rt.palmimo.display is None
    assert rt.palmimo.has_connectable_resource is False
    assert rt.speech is None
    assert rt.vision_watch is None
    assert rt.reflexes is None


def test_build_runtime_registers_the_companion_toolset_even_without_hardware() -> None:
    rt = build_runtime(_settings(hardware=False))

    assert "dance" in rt.toolset.tool_names
    assert "wave" not in rt.toolset.tool_names
    assert "creep" not in rt.toolset.tool_names


# ----------------------------------------------------------------------
# hardware=True -- every peripheral is built unconditionally, no probing
# ----------------------------------------------------------------------


def test_build_runtime_wires_a_palmimo_with_every_peripheral(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _patch_all_peripherals(monkeypatch)

    rt = build_runtime(_settings(hardware=True))

    assert isinstance(rt.palmimo, Palmimo)
    assert rt.palmimo.driver is fakes["driver"]
    assert rt.palmimo.display is fakes["display"]
    assert rt.palmimo.speaker is fakes["speaker"]
    assert rt.palmimo.camera is fakes["camera"]
    assert rt.palmimo.mic is fakes["mic"]

    assert isinstance(rt.speech, FakeSpeechPipeline)
    assert rt.speech.kwargs["mic"] is fakes["mic"]

    assert isinstance(rt.vision_watch, FakeVisionWatch)
    assert rt.vision_watch.camera is fakes["camera"]
    # The wave detector plus the face-presence adapter over the same locator
    # look_at_face polls -- both reflexes need a producer.
    assert rt.vision_watch.detectors[0] is fakes["wave_detector"]
    [face_detector] = rt.vision_watch.detectors[1:]
    assert isinstance(face_detector, FacePresenceDetector)
    assert face_detector._locator is fakes["face_locator"]
    assert isinstance(rt.reflexes, ReflexEngine)


def test_build_runtime_shares_one_sleeping_mirror_between_conductor_and_reflexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reflex must not wave/track a sleeping robot -- reflexes.inhibited should
    track the same Sleeping mirror the conductor gates its own idle loop with."""
    _patch_all_peripherals(monkeypatch)

    rt = build_runtime(_settings(hardware=True))

    assert rt.reflexes is not None
    assert rt.reflexes._inhibited() is False

    rt.history.add(ToolExecEvent(tool_call_id="t1", name="sleep", arguments="{}", result="went to sleep"))

    assert rt.conductor.sleeping.asleep is True
    assert rt.reflexes._inhibited() is True


def test_build_runtime_injects_the_face_locator_into_look_at_face(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _patch_all_peripherals(monkeypatch)

    rt = build_runtime(_settings(hardware=True))

    # Injected via a per-runtime subclass (make_look_at_face_tool), not a
    # ClassVar assignment on the shared LookAtFaceTool -- see
    # test_build_runtime_does_not_leak_the_face_locator_into_the_shared_base_class
    # and test_build_runtime_look_at_face_locators_do_not_leak_between_two_runtimes.
    registered = rt.toolset.tool_models["look_at_face"]
    assert registered._face_locator is fakes["face_locator"]


def test_build_runtime_does_not_leak_the_face_locator_into_the_shared_base_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The process-global LookAtFaceTool base class must stay untouched --
    only the per-runtime subclass carries the locator."""
    _patch_all_peripherals(monkeypatch)

    build_runtime(_settings(hardware=True))

    assert LookAtFaceTool._face_locator is None


def test_build_runtime_look_at_face_locators_do_not_leak_between_two_runtimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two Runtimes built in the same process (e.g. two tests) must each keep
    their own face locator rather than clobbering each other's through a
    shared ClassVar."""
    fakes_1 = _patch_all_peripherals(monkeypatch)
    rt1 = build_runtime(_settings(hardware=True))

    fakes_2 = _patch_all_peripherals(monkeypatch)
    rt2 = build_runtime(_settings(hardware=True))

    tool_1 = rt1.toolset.tool_models["look_at_face"]
    tool_2 = rt2.toolset.tool_models["look_at_face"]
    assert tool_1 is not tool_2
    assert tool_1._face_locator is fakes_1["face_locator"]
    assert tool_2._face_locator is fakes_2["face_locator"]


def test_build_runtime_passes_the_configured_port_to_the_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _driver_factory(*, port: str | None = None) -> FakeDriver:
        captured["port"] = port
        return FakeDriver(port=port)

    monkeypatch.setattr(wiring, "DynamixelDriver", _driver_factory)
    monkeypatch.setattr(wiring, "FaceDisplay", FakePeripheral)
    monkeypatch.setattr(wiring, "Speaker", lambda config, **_kwargs: FakePeripheral())
    monkeypatch.setattr(wiring, "HeadCamera", FakePeripheral)
    monkeypatch.setattr(wiring, "MicStream", FakePeripheral)
    monkeypatch.setattr(wiring, "FaceLocator", object)
    monkeypatch.setattr(wiring, "WaveDetector", object)
    monkeypatch.setattr(wiring, "SpeechPipeline", FakeSpeechPipeline)

    build_runtime(_settings(hardware=True, port="/dev/ttyACM0"))

    assert captured["port"] == "/dev/ttyACM0"


def _speaker_config_from(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> SpeakerConfig:
    """Build a hardware runtime and return the config wiring handed to ``Speaker``."""
    _patch_all_peripherals(monkeypatch)
    captured: list[SpeakerConfig] = []

    def _speaker_factory(config: SpeakerConfig, **_kwargs: object) -> FakePeripheral:
        captured.append(config)
        return FakePeripheral()

    monkeypatch.setattr(wiring, "Speaker", _speaker_factory)

    build_runtime(_settings(hardware=True, **overrides))

    [config] = captured
    return config


def test_build_runtime_names_the_playback_device_for_the_speaker(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _speaker_config_from(monkeypatch, speaker_device="ReSpeaker", language="ja")

    assert config.device_name_hint == "ReSpeaker"
    assert config.lang == "ja"


def test_build_runtime_leaves_the_playback_device_to_alsa_when_the_name_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An emptied setting must reach the config as a falsy hint, which is how
    SpeakerConfig spells "issue aplay without -D" -- not be replaced by the
    field default on its way through wiring."""
    config = _speaker_config_from(monkeypatch, speaker_device="")

    assert not config.device_name_hint


# ----------------------------------------------------------------------
# Runtime.connect / Runtime.start / Runtime.aclose
# ----------------------------------------------------------------------


class FakeConductor:
    def __init__(self) -> None:
        self.run_started = False
        self.cancelled = False

    async def run(self) -> None:
        self.run_started = True
        try:
            await asyncio.sleep(1000)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_runtime_connect_skips_connect_for_a_compute_only_palmimo() -> None:
    """A compute-only Palmimo (hardware=False) has nothing to connect -- connect() on it would raise."""
    palmimo = Palmimo()
    conductor = FakeConductor()
    rt = Runtime(conductor=conductor, palmimo=palmimo, toolset=None, history=None, event_log=None)

    await rt.connect()  # must not raise

    await rt.aclose()


async def test_runtime_start_connects_the_robot_and_launches_the_conductor_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    palmimo = Palmimo()
    connect_calls = []
    monkeypatch.setattr(type(palmimo), "has_connectable_resource", property(lambda self: True))
    monkeypatch.setattr(palmimo, "connect", lambda: connect_calls.append(1))
    conductor = FakeConductor()
    rt = Runtime(conductor=conductor, palmimo=palmimo, toolset=None, history=None, event_log=None)

    await rt.start()
    await asyncio.sleep(0)  # let the conductor task actually start running

    assert connect_calls == [1]
    assert conductor.run_started is True

    await rt.aclose()


async def test_runtime_start_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    palmimo = Palmimo()
    connect_calls = []
    monkeypatch.setattr(type(palmimo), "has_connectable_resource", property(lambda self: True))
    monkeypatch.setattr(palmimo, "connect", lambda: connect_calls.append(1))
    conductor = FakeConductor()
    rt = Runtime(conductor=conductor, palmimo=palmimo, toolset=None, history=None, event_log=None)

    await rt.start()
    await rt.start()

    assert connect_calls == [1]
    await rt.aclose()


async def test_runtime_connect_then_start_connects_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirrors ui.tui.run_tui: connect() called on its own loop, then start() from another."""
    palmimo = Palmimo()
    connect_calls = []
    monkeypatch.setattr(type(palmimo), "has_connectable_resource", property(lambda self: True))
    monkeypatch.setattr(palmimo, "connect", lambda: connect_calls.append(1))
    conductor = FakeConductor()
    rt = Runtime(conductor=conductor, palmimo=palmimo, toolset=None, history=None, event_log=None)

    await rt.connect()
    await rt.start()

    assert connect_calls == [1]
    await rt.aclose()


async def test_runtime_start_propagates_a_connect_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hardware connect failure must not be swallowed -- it is the caller's job to report it."""
    palmimo = Palmimo()
    monkeypatch.setattr(type(palmimo), "has_connectable_resource", property(lambda self: True))

    def _raise() -> None:
        raise RuntimeError("servo bus not found")

    monkeypatch.setattr(palmimo, "connect", _raise)
    conductor = FakeConductor()
    rt = Runtime(conductor=conductor, palmimo=palmimo, toolset=None, history=None, event_log=None)

    with pytest.raises(RuntimeError, match="servo bus not found"):
        await rt.start()

    assert conductor.run_started is False  # the conductor task must never launch after a failed connect


async def test_runtime_aclose_cancels_tasks_disconnects_the_robot_and_closes_the_event_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    palmimo = Palmimo()
    monkeypatch.setattr(type(palmimo), "has_connectable_resource", property(lambda self: True))
    monkeypatch.setattr(palmimo, "connect", lambda: None)
    disconnect_calls = []
    monkeypatch.setattr(palmimo, "disconnect", lambda: disconnect_calls.append(1))
    conductor = FakeConductor()
    log = io.StringIO()
    rt = Runtime(conductor=conductor, palmimo=palmimo, toolset=None, history=None, event_log=log)

    await rt.start()
    await asyncio.sleep(0)
    await rt.aclose()

    assert conductor.cancelled is True
    assert disconnect_calls == [1]
    assert log.closed is True


async def test_runtime_aclose_without_start_does_not_raise() -> None:
    palmimo = Palmimo()
    conductor = FakeConductor()
    rt = Runtime(conductor=conductor, palmimo=palmimo, toolset=None, history=None, event_log=None)

    await rt.aclose()  # must not raise even though start() was never called


# ----------------------------------------------------------------------
# _build_engine
# ----------------------------------------------------------------------


def test_build_engine_defaults_to_piper() -> None:
    assert wiring._build_engine(_settings()).name == "piper"


def test_build_engine_rejects_an_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown voice_backend"):
        wiring._build_engine(_settings(voice_backend="espeak"))


def test_build_engine_leaves_the_voice_name_to_the_backend_when_unset() -> None:
    """Neither engine takes None as "your default", so an unset name has to
    mean the argument is never passed."""
    assert wiring._build_engine(_settings())._model_ja == "ja_JP-tsukuyomi-chan-medium"


def test_build_engine_passes_the_voice_name_to_piper() -> None:
    piper = wiring._build_engine(_settings(voice_name="ja_JP-css10-6lang-medium"))
    assert piper._model_ja == "ja_JP-css10-6lang-medium"


def test_build_engine_returns_openai_when_selected() -> None:
    assert wiring._build_engine(_settings(voice_backend="openai")).name == "openai"


def test_build_engine_passes_the_voice_name_to_openai() -> None:
    assert wiring._build_engine(_settings(voice_backend="openai", voice_name="cedar"))._voice == "cedar"


def test_build_engine_inverts_speed_for_piper_only() -> None:
    """voice_speed is "higher is faster", which is the openai engine's sense
    and the inverse of piper's length_scale -- so the same setting has to
    reach the two engines as reciprocals of each other."""
    assert wiring._build_engine(_settings(voice_speed=0.625))._length_scale == pytest.approx(1.6)
    assert wiring._build_engine(_settings(voice_backend="openai", voice_speed=1.3))._speed == pytest.approx(1.3)


# ----------------------------------------------------------------------
# _build_mic
# ----------------------------------------------------------------------


class _RecordingMicStream:
    """Stands in for MicStream, keeping whatever processors it was handed."""

    def __init__(self, **kwargs: object) -> None:
        self.processors = kwargs.get("processors")


def test_build_mic_attaches_an_echo_canceller_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[dict[str, object]] = []

    def _canceller(**kwargs: object) -> str:
        built.append(kwargs)
        return "canceller"

    monkeypatch.setattr(wiring, "MicStream", _RecordingMicStream)
    monkeypatch.setattr(wiring, "EchoCanceller", _canceller)

    mic = wiring._build_mic(_settings())

    assert mic.processors == ["canceller"]
    assert built == [{"near_channel": 0, "reference_channel": 5}]


def test_build_mic_passes_the_configured_channels_through(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[dict[str, object]] = []

    def _canceller(**kwargs: object) -> str:
        built.append(kwargs)
        return "canceller"

    monkeypatch.setattr(wiring, "MicStream", _RecordingMicStream)
    monkeypatch.setattr(wiring, "EchoCanceller", _canceller)

    wiring._build_mic(_settings(near_channel=1, reference_channel=4))

    assert built == [{"near_channel": 1, "reference_channel": 4}]


def test_build_mic_leaves_capture_raw_when_echo_cancel_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty list, never None: MicStream reads None as "use my default",
    and its default is an EchoCanceller -- so passing nothing would turn
    cancellation on rather than off."""

    def _should_not_build_a_canceller(**kwargs: object) -> object:
        raise AssertionError(f"EchoCanceller must not be built: {kwargs}")

    monkeypatch.setattr(wiring, "MicStream", _RecordingMicStream)
    monkeypatch.setattr(wiring, "EchoCanceller", _should_not_build_a_canceller)

    assert wiring._build_mic(_settings(echo_cancel=False)).processors == []
