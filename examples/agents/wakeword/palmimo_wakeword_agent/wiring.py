"""Wires the real Silero VAD, Whisper transcriber, wake-word detector, and
OpenAI tool-calling agent onto a live :class:`~palmimo_sdk.MicStream` and
:class:`~palmimo_sdk.Palmimo`.

Kept out of :mod:`palmimo_wakeword_agent.main` so the demo's actual story --
the flat listen loop -- is the first thing a reader sees. A
:class:`~palmimo_sdk.DynamixelDriver` is attached by default (see
:func:`_build_servo_driver`) so commands move a real robot; it degrades to
compute-only when the servo bus can't be reached.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from openai import OpenAI

from palmimo_sdk import MicStream, Palmimo, PortDetectionError, Speaker, SpeakerConfig
from palmimo_sdk.agent import AgentToolSet

from .agent import ChatClientLike, CommandAgent
from .settings import WakewordAgentSettings
from .stt import WhisperStt
from .vad import SileroVad, UtteranceSegmenter
from .wakeword import WakeWordDetector


if TYPE_CHECKING:
    from palmimo_sdk import ServoDriver, SpeechHandle


logger = logging.getLogger(__name__)

#: Google's OpenAI-compatible endpoint: chat.completions + tool calling work
#: against it, so a Gemini `command_model` can reuse the same `openai.OpenAI`
#: client shape as a plain OpenAI model -- only the base_url/api_key differ.
_GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class _ServoDriverLike(Protocol):
    """Structural surface of a :class:`~palmimo_sdk.DynamixelDriver` we depend on here."""

    def connect(self) -> None: ...


def _build_servo_driver(
    settings: WakewordAgentSettings,
    driver_factory: Callable[..., _ServoDriverLike] | None = None,
) -> _ServoDriverLike | None:
    """Build and probe-connect the servo driver, degrading to compute-only on failure.

    Args:
        settings: Resolved agent settings (``servo`` / ``servo_port``).
        driver_factory: Builds the driver from ``port=...`` (dependency-injected
            for tests). ``None`` (the default) lazily imports and uses
            :class:`~palmimo_sdk.DynamixelDriver`.

    Returns:
        The connected driver, or ``None`` when ``settings.servo`` is ``False``,
        or when the servo bus could not be reached -- no hardware attached, the
        port couldn't be auto-detected, an explicit ``--servo-port`` was wrong,
        or (without the SDK's ``hardware`` extra installed) ``dynamixel_sdk``
        itself is missing. A one-line warning is printed in every failure case
        instead of raising, so the agent still starts compute-only.

    ``DynamixelDriver.__init__`` never touches hardware: port auto-detection and
    the actual serial handshake both happen inside :meth:`connect`. So probing
    is only possible by calling ``connect()`` here explicitly -- the same
    pattern :func:`build_runtime` already uses to pre-probe the ``Speaker``
    before building ``Palmimo``. The failure can surface as
    :class:`~palmimo_sdk.PortDetectionError` (zero or multiple port
    candidates) or, more generally, any other :class:`Exception` (a
    serial-layer error opening the port, a missing ``dynamixel_sdk`` import,
    etc.), so both are caught.
    """
    if not settings.servo:
        return None

    if driver_factory is None:
        from palmimo_sdk import DynamixelDriver

        driver_factory = DynamixelDriver
    driver = driver_factory(port=settings.servo_port)
    try:
        driver.connect()
    except PortDetectionError as exc:
        print(f"servo bus not available -- motions run compute-only ({exc})")
        return None
    except Exception as exc:  # missing hardware extra, serial-layer error, etc.
        print(f"servo bus not available -- motions run compute-only ({exc})")
        return None
    return driver


def _chat_client_for(settings: WakewordAgentSettings, openai_client: OpenAI) -> OpenAI:
    """Pick the chat-completions client for ``settings.command_model``.

    A ``gemini-``-prefixed model is routed to Google's OpenAI-compatible
    endpoint with ``GEMINI_API_KEY``; anything else (plain OpenAI models,
    e.g. ``gpt-5-nano``) reuses *openai_client* as-is, so overriding
    ``command_model`` back to an OpenAI model needs only ``OPENAI_API_KEY``.
    Pure and hardware-free (no network I/O) so it's cheap to unit-test.
    """
    if settings.command_model.startswith("gemini-"):
        return OpenAI(api_key=settings.gemini_api_key, base_url=_GEMINI_OPENAI_BASE_URL)
    return openai_client


@dataclass
class Runtime:
    """Every live dependency :func:`~palmimo_wakeword_agent.main.listen_loop` needs."""

    robot: Palmimo
    mic: MicStream
    segmenter: UtteranceSegmenter
    detector: WakeWordDetector
    stt: WhisperStt
    agent: CommandAgent
    say: Callable[[str], None]
    is_speaking: Callable[[], bool]


def build_runtime(settings: WakewordAgentSettings) -> Runtime:
    """Build every real component and wire them into a ready-to-run :class:`Runtime`."""
    print("Preparing models (first run downloads ~2 MB)...")

    detector = WakeWordDetector()
    vad = SileroVad.load()
    segmenter = UtteranceSegmenter(vad=vad)
    openai_client = OpenAI(api_key=settings.openai_api_key)
    stt = WhisperStt(openai_client, model=settings.stt_model, language=settings.language)

    speaker: Speaker | None = None
    if settings.tts:
        speaker = Speaker(SpeakerConfig(lang=settings.language, device_name_hint=settings.speaker_device))
        try:
            speaker.open()  # probe now, before Palmimo(speaker=...) re-probes it for free
        except RuntimeError:
            print("piper not available -- spoken replies disabled")
            speaker = None

    driver = _build_servo_driver(settings)

    # The facade owns every I/O resource — servo bus, speaker, AND the mic —
    # so one connect()/disconnect() manages them together (with partial-failure
    # rollback). The example keeps a direct `mic` reference only to call
    # `stream()` on it, which the `robot.mic` property's static type
    # (`Microphone | MicStream | None`) can't offer without narrowing.
    mic = MicStream(device_name_hint=settings.device)
    # `_build_servo_driver` returns the narrow `_ServoDriverLike` Protocol (only
    # `connect()`) so tests can inject a lightweight fake; the real default
    # factory (`DynamixelDriver`) is a true `ServoDriver`, so the cast is safe.
    robot = Palmimo(driver=cast("ServoDriver | None", driver), speaker=speaker, mic=mic)
    toolset = AgentToolSet(robot)
    # `openai.OpenAI` structurally satisfies `ChatClientLike` at runtime, but mypy can't
    # verify it: newer `openai` SDK versions type `message.tool_calls` as a union that also
    # includes a custom-tool-call variant lacking `.function`, which `_ToolCallLike` (by
    # design, since this agent only ever emits function tool calls) does not accept. Cast
    # rather than loosen the protocol, so `CommandAgent.run`'s own logic stays precisely typed.
    chat_client = _chat_client_for(settings, openai_client)
    agent = CommandAgent(
        cast("ChatClientLike", chat_client), toolset, model=settings.command_model, language=settings.language
    )

    # Self-trigger suppression state: the last SpeechHandle we started, so
    # incoming mic chunks are dropped (and the segmenter reset) while our own
    # TTS is still speaking -- never segment the robot's own voice.
    speaking_handle: SpeechHandle | None = None

    def say(text: str) -> None:
        nonlocal speaking_handle
        print(f"[palmimo] {text}")
        speaking_handle = robot.say(text)

    def is_speaking() -> bool:
        return speaking_handle is not None and speaking_handle.is_alive()

    return Runtime(
        robot=robot,
        mic=mic,
        segmenter=segmenter,
        detector=detector,
        stt=stt,
        agent=agent,
        say=say,
        is_speaking=is_speaking,
    )
