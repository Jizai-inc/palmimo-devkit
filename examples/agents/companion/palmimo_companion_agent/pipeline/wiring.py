"""Builds every real dependency and wires them into a ready-to-run :class:`Runtime`.

Kept out of :mod:`palmimo_companion_agent.main` / :mod:`.ui.cli` / :mod:`.ui.tui`
so the front ends stay thin. Assembly only -- never probes a peripheral,
never decides whether the robot is usable. ``hardware=True`` (default)
unconditionally constructs the full peripheral set and hands it to
:class:`~palmimo_sdk.Palmimo`; whether it actually works is entirely
:meth:`~palmimo_sdk.robot.Palmimo.connect`'s call (all-or-nothing -- see its
own docstring). ``hardware=False`` wires a hardware-free, compute-only
Palmimo instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TextIO

from palmimo_sdk import (
    DynamixelDriver,
    EchoCanceller,
    FaceDisplay,
    HeadCamera,
    MicStream,
    OpenAiEngine,
    Palmimo,
    PiperEngine,
    Speaker,
    SpeakerConfig,
    TtsEngine,
)
from palmimo_sdk.agent.toolset import AgentToolSet

from ..core.hearing import NameCallWatch
from ..core.perception import merge
from ..core.reflexes import ReflexEngine
from ..core.tools import COMPANION_TOOL_MODELS, make_look_at_face_tool
from ..core.vision import FaceLocator, FacePresenceDetector, VisionWatch, WaveDetector
from .bus import Bus
from .conductor import Conductor
from .history import History, SystemNoteEvent
from .llm import LlmProvider
from .speech import SpeechPipeline


if TYPE_CHECKING:
    from .settings import PipelineSettings


_log = logging.getLogger(__name__)

#: LiteLLM model-string provider prefix -> the environment variable that must
#: hold its API key. Only providers this example actually configures by
#: default (gemini / openai) are checked -- an unrecognized provider prefix
#: (e.g. a self-hosted model) is assumed to need no key here.
_PROVIDER_API_KEY_ENV: dict[str, str] = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
}

#: Tools excluded from the underlying AgentToolSet before this agent's own
#: overrides/composites are registered on top of it -- see
#: :mod:`palmimo_companion_agent.core.tools` module docstring. Neither is
#: exposed to the LLM: bare `wave` (one arm) has no companion tool of its
#: own (wave_both covers the greeting gesture, and the wave-back reflex --
#: see :mod:`palmimo_companion_agent.core.reflexes` -- calls wave_both too,
#: not wave), and `creep` has no companion use case either.
_EXCLUDED_SDK_TOOLS = ("creep", "wave")


def _preflight_api_keys(settings: PipelineSettings) -> None:
    """Fail fast with a clear message if a configured model's API key is missing.

    The only startup preflight this example performs: LiteLLM reads
    ``GEMINI_API_KEY``/``OPENAI_API_KEY`` straight from the process
    environment, so a missing key would otherwise only surface as an opaque
    auth error deep inside the first LLM call. Walks every configured model
    string and checks its provider's key upfront.

    Raises:
        RuntimeError: One or more required API key environment variables are
            not set.
    """
    models = {
        "chat_model": settings.chat_model,
        "guard_model": settings.guard_model,
        "vlm_model": settings.vlm_model,
        "stt_model": settings.stt_model,
    }
    missing_vars: set[str] = set()
    for model in models.values():
        provider = model.split("/", 1)[0]
        env_var = _PROVIDER_API_KEY_ENV.get(provider)
        if env_var is not None and not os.environ.get(env_var):
            missing_vars.add(env_var)
    if missing_vars:
        raise RuntimeError(
            "Missing required API key environment variable(s): "
            f"{', '.join(sorted(missing_vars))}. Set them in the process "
            "environment or in .env (see .env.sample) before starting the agent."
        )


def _build_mic(settings: PipelineSettings) -> MicStream:
    """Build the capture stream, with echo cancellation if it is enabled.

    ``EchoCanceller`` declares how many channels it needs and MicStream opens
    that many, so building one here is what makes capture use the array's
    full channel set.

    Both branches pass ``processors`` explicitly, including the empty list:
    MicStream reads ``processors=None`` as "use my default", and its default
    is an ``EchoCanceller`` of its own -- so omitting the argument to turn
    cancellation off would turn it on instead.
    """
    if not settings.echo_cancel:
        return MicStream(processors=[])
    canceller = EchoCanceller(
        near_channel=settings.near_channel,
        reference_channel=settings.reference_channel,
    )
    return MicStream(processors=[canceller])


def _build_engine(settings: PipelineSettings) -> TtsEngine:
    """Build the TTS engine named by ``settings.voice_backend``.

    ``voice_speed`` is normalized as "1.0 is natural, higher is faster",
    which is the OpenAI engine's own sense and the inverse of piper's
    ``length_scale`` -- hence the reciprocal.

    Raises:
        ValueError: ``voice_backend`` is not a backend this agent knows.
    """
    # The override dicts are Any-valued because an absent key means "keep the
    # engine's own default", and ** unpacking that cannot be typed precisely
    # without a TypedDict per engine.
    if settings.voice_backend == "openai":
        voice: dict[str, Any] = {"voice": settings.voice_name} if settings.voice_name else {}
        return OpenAiEngine(speed=settings.voice_speed, volume=settings.voice_volume, **voice)
    if settings.voice_backend == "piper":
        model: dict[str, Any] = {"model_ja": settings.voice_name} if settings.voice_name else {}
        return PiperEngine(
            data_dir=settings.voice_dir,
            length_scale=1.0 / settings.voice_speed,
            volume=settings.voice_volume,
            **model,
        )
    raise ValueError(f"unknown voice_backend {settings.voice_backend!r}; expected 'piper' or 'openai'")


@dataclass
class Runtime:
    """Every live dependency the companion agent's front ends (CLI / TUI) need.

    :meth:`start` connects the robot and launches every background task this
    runtime owns (the conductor loop, and -- when hardware provided them --
    the speech pipeline and the vision-reflex loop); :meth:`aclose` reverses
    that. :meth:`connect` is split out from :meth:`start` so a front end with
    its own event loop (the Textual TUI) can connect on a throwaway loop
    *before* handing control to it -- see
    :func:`palmimo_companion_agent.pipeline.ui.tui.run_tui`'s docstring for why.
    """

    conductor: Conductor
    palmimo: Palmimo
    toolset: AgentToolSet
    history: History
    event_log: TextIO | None = None
    speech: SpeechPipeline | None = None
    vision_watch: VisionWatch | None = None
    hearing_watch: NameCallWatch | None = None
    reflexes: ReflexEngine | None = None
    _tasks: list[asyncio.Task] = field(default_factory=list, repr=False)
    _connected: bool = field(default=False, repr=False)
    _started: bool = field(default=False, repr=False)

    async def connect(self) -> None:
        """Connect the robot. Idempotent: a second call (including from :meth:`start`) is a no-op.

        A compute-only facade (``hardware=False``) has nothing to connect --
        calling ``connect()`` on one would itself raise (see
        :attr:`~palmimo_sdk.robot.Palmimo.has_connectable_resource`), so this
        skips the call entirely in that case. Otherwise runs on a worker
        thread via :func:`asyncio.to_thread`: ``Palmimo.connect()`` is
        blocking (it may run a multi-second wake glide) and all-or-nothing
        (see its own docstring).
        """
        if self._connected:
            return
        self._connected = True
        if self.palmimo.has_connectable_resource:
            await asyncio.to_thread(self.palmimo.connect)

    async def start(self) -> None:
        """Connect the robot (if not already) and launch every background task this runtime owns.

        Idempotent: a second call is a no-op (a front end calling this once
        up front should never accidentally double-launch the loops).
        """
        if self._started:
            return
        self._started = True
        await self.connect()
        self._tasks.append(asyncio.ensure_future(self.conductor.run()))
        if self.speech is not None:
            self._tasks.append(asyncio.ensure_future(self.speech.run()))
        if self.reflexes is not None:
            # One engine over both senses: which one noticed is the detection's
            # own business, and arbitration (busy, asleep, cooldown) has to see
            # them together to be arbitration at all.
            senses = [w.watch() for w in (self.vision_watch, self.hearing_watch) if w is not None]
            if senses:
                self._tasks.append(asyncio.ensure_future(self.reflexes.run(merge(*senses))))

    async def aclose(self) -> None:
        """Cancel every task this runtime launched, disconnect the robot, close the event log.

        Best-effort and idempotent-safe regardless of which peripherals were
        attached -- unlike :meth:`connect`, this doesn't gate on
        ``has_connectable_resource``: ``Palmimo.disconnect()`` is always safe
        to call (even compute-only, or never connected).
        """
        if self.vision_watch is not None:
            with contextlib.suppress(Exception):
                await self.vision_watch.aclose()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task
        self._tasks.clear()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.palmimo.disconnect)
        if self.event_log is not None:
            with contextlib.suppress(Exception):
                self.event_log.close()


def build_runtime(settings: PipelineSettings) -> Runtime:
    """Build every real component and wire them into a ready-to-run :class:`Runtime`.

    See the module docstring for the ``hardware`` true/false split.

    Raises:
        RuntimeError: A configured model's API key environment variable is
            missing (see :func:`_preflight_api_keys`).
    """
    _preflight_api_keys(settings)

    event_log: TextIO | None = None
    if settings.log_path is not None:
        settings.log_path.parent.mkdir(parents=True, exist_ok=True)
        event_log = settings.log_path.open("a", encoding="utf-8")

    history = History()
    bus = Bus()
    llm = LlmProvider.from_settings(settings)

    mic: MicStream | None = None
    camera: HeadCamera | None = None
    face_locator: FaceLocator | None = None
    wave_detector: WaveDetector | None = None

    if not settings.hardware:
        palmimo = Palmimo()
    else:
        driver = DynamixelDriver(port=settings.port)
        display = FaceDisplay()
        speaker = Speaker(
            SpeakerConfig(lang=settings.language, device_name_hint=settings.speaker_device),
            engine=_build_engine(settings),
        )
        camera = HeadCamera()
        mic = _build_mic(settings)
        face_locator = FaceLocator()
        wave_detector = WaveDetector()
        palmimo = Palmimo(driver=driver, display=display, speaker=speaker, camera=camera, mic=mic)

    toolset = AgentToolSet(palmimo, exclude=_EXCLUDED_SDK_TOOLS)
    # Shared, hardware-scoped infrastructure (a MediaPipe model instance), not
    # a per-call LLM argument -- bound via a per-runtime subclass
    # (make_look_at_face_tool) rather than a ClassVar assignment on the
    # shared LookAtFaceTool, so two Runtimes built in the same process never
    # clobber each other's locator. None with hardware=False / no camera,
    # same as every other peripheral-gated tool behavior.
    tool_models = dict(COMPANION_TOOL_MODELS)
    tool_models["look_at_face"] = make_look_at_face_tool(face_locator)
    for cls in tool_models.values():
        toolset.register(cls)

    conductor = Conductor(history, toolset, llm, bus, event_log=event_log)

    speech: SpeechPipeline | None = None
    if mic is not None:
        speech = SpeechPipeline(
            mic=mic,
            llm=llm,
            conductor=conductor,
            language=settings.language,
            silence_seconds=settings.silence_seconds,
        )

    vision_watch: VisionWatch | None = None
    if camera is not None:
        assert face_locator is not None and wave_detector is not None  # both built alongside camera above
        # The same locator instance look_at_face polls; FaceLocator serializes
        # its MediaPipe calls, so sharing it with FacePresenceDetector is safe.
        detectors: list[WaveDetector | FacePresenceDetector] = [wave_detector, FacePresenceDetector(face_locator)]
        vision_watch = VisionWatch(camera, detectors)

    # Hearing needs no hardware of its own: the speech pipeline is already
    # transcribing, so this reads that text. It is therefore wired whenever
    # there is a mic, independently of whether a camera exists.
    hearing_watch: NameCallWatch | None = None
    if speech is not None:
        hearing_watch = NameCallWatch()
        speech.set_transcript_observer(hearing_watch.heard)

    reflexes: ReflexEngine | None = None
    if vision_watch is not None or hearing_watch is not None:
        # Shares the Conductor's own Sleeping mirror (rather than building a
        # second one) so a reflex is inhibited by the same asleep state the
        # idle loop gates against -- see core/reflexes.py's module docstring.
        reflexes = ReflexEngine(
            toolset,
            lambda text: history.add(SystemNoteEvent(text)),
            inhibited=lambda: conductor.sleeping.asleep,
        )

    return Runtime(
        conductor=conductor,
        palmimo=palmimo,
        toolset=toolset,
        history=history,
        event_log=event_log,
        speech=speech,
        vision_watch=vision_watch,
        hearing_watch=hearing_watch,
        reflexes=reflexes,
    )


__all__ = ["Runtime", "build_runtime"]
