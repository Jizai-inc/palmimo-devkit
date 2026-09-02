"""Shared settings base for the companion agent, backed by pydantic-settings.

Deliberately simple: one ``BaseSettings`` class, no YAML, no custom sources.
Tuning thresholds and word lists have no place here -- they are constants in
whichever module needs them (e.g. the LLM provider's fallback model), not
settings fields.

:class:`CompanionSettings` holds only the fields every runtime needs
(hardware attach, servo port, hearing and playback-device knobs, reply
language, event log path) -- the character is one thing, but "one character, two runtimes" (see the
package's own README) means each runtime's own knobs (the pipeline chat/
guard/VLM/STT models and voice backend today; a future realtime/ runtime's
own surface later) belong on that runtime's own settings subclass instead of
here. See :class:`~palmimo_companion_agent.pipeline.settings.PipelineSettings`
for the cascaded runtime's.

Precedence (highest wins):
  1. keyword overrides passed to :func:`load_settings` / ``with_env_file``
  2. process environment (``COMPANION_AGENT_*``)
  3. the project ``.env`` file
  4. field default

``GEMINI_API_KEY`` / ``OPENAI_API_KEY`` are read directly by LiteLLM from the
process environment and are deliberately not modeled as settings fields (see
:meth:`CompanionSettings.with_env_file`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict


#: The companion agent example's project directory (parent of this package),
#: where ``.env`` / ``.env.sample`` live. Each runtime's own settings module
#: (e.g. pipeline/settings.py) derives its own default ``.env`` path from
#: this rather than this module keeping one of its own -- CompanionSettings
#: itself has no ``load_settings()`` entry point to need it for.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

_SettingsT = TypeVar("_SettingsT", bound="CompanionSettings")


class CompanionSettings(BaseSettings):
    """Runtime configuration shared by every companion agent runtime."""

    model_config = SettingsConfigDict(
        env_prefix="COMPANION_AGENT_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    #: False runs fully compute-only: every peripheral (servos, camera,
    #: display, speaker) is stubbed, no hardware is touched.
    hardware: bool = True

    #: Servo bus serial port (e.g. ``/dev/ttyACM0``); ``None`` auto-detects it.
    port: str | None = None

    #: Cancel the loudspeaker out of the microphone, so the robot can hear
    #: while it is talking. Needs a capture device that exposes the
    #: loudspeaker as a loopback channel.
    echo_cancel: bool = True

    #: Microphone channel the canceller cleans and returns. 0 is the array
    #: chip's processed output, which carries the gain that makes voice
    #: detection fire reliably.
    near_channel: int = 0

    #: Loudspeaker loopback channel used as the canceller's reference.
    reference_channel: int = 5

    #: Substring naming the playback device (``SpeakerConfig.device_name_hint``),
    #: matched against the ALSA card id and its long name. The default names
    #: the mic array the two channels above already assume: it carries the
    #: loudspeaker, and its loopback is what the canceller reads -- speech sent
    #: to another output leaves the canceller nothing to cancel. Naming it
    #: rather than taking ALSA's default is what survives a replug, since a
    #: card *index* is a property of one boot's enumeration order and the id is
    #: not. Empty or unmatched falls back to ALSA's default (a warning is
    #: logged), which is the behavior of a machine with no array attached.
    speaker_device: str | None = "ReSpeaker"

    #: ISO 639-1 language code driving the reply language and STT hint.
    language: str = "ja"

    #: JSONL event log destination; ``None`` disables event logging.
    log_path: Path | None = None

    @classmethod
    def with_env_file(cls: type[_SettingsT], env_file: Path | str | None, **field_values: object) -> _SettingsT:
        """Build settings reading *env_file* instead of the project ``.env``.

        Also loads *env_file* into the process environment (best-effort) via
        ``python-dotenv``: ``GEMINI_API_KEY`` / ``OPENAI_API_KEY`` are not
        settings fields, so this is the only path that makes an .env-supplied
        key visible to LiteLLM's own environment-variable lookup. ``None``
        disables both effects entirely (test isolation) -- a developer's real
        ``.env`` cannot leak in.

        Generic over ``cls`` (a plain ``@classmethod``, not tied to
        :class:`CompanionSettings` itself) so a subclass -- e.g.
        :class:`~palmimo_companion_agent.pipeline.settings.PipelineSettings`
        -- inherits it and returns its own type.
        """
        if env_file is not None:
            load_dotenv(env_file, override=False)
        return cls(_env_file=env_file, **field_values)  # type: ignore[call-arg, arg-type]  # pyright: ignore[reportCallIssue]
