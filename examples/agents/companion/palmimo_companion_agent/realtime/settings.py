"""Settings for the Realtime voice runtime.

Adds this runtime's own knobs -- the Realtime model/voice, pitch, reply
length hint, camera-frame cadence, and session length -- on top of
:class:`~palmimo_companion_agent.settings.CompanionSettings`'s shared base
(hardware attach, servo port, hearing knobs, language, log path). See that
module's docstring for the "one character, two runtimes" split this mirrors;
compare :class:`~palmimo_companion_agent.pipeline.settings.PipelineSettings`
for the cascaded runtime's own equivalent.
"""

from __future__ import annotations

from pydantic import Field, field_validator

from ..settings import PROJECT_ROOT, CompanionSettings


_DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"

#: The voice cannot be changed once a session has spoken, so it is chosen at
#: startup. ``marin`` and ``cedar`` are the two OpenAI recommends.
VOICES = ("alloy", "ash", "ballad", "cedar", "coral", "echo", "marin", "sage", "shimmer", "verse")


class RealtimeSettings(CompanionSettings):
    """Runtime configuration for the OpenAI Realtime voice front end."""

    #: OpenAI Realtime API model string.
    model: str = "gpt-realtime-2.1"

    #: Voice for the session's own spoken audio.
    voice: str = "coral"

    #: Playback pitch multiplier; raises the voice to suit a small creature.
    #: Resampling shifts pitch and tempo together, which is the intended
    #: effect here and costs no latency. 1.0 leaves the voice unshifted.
    pitch: float = Field(default=1.15, gt=0)

    #: Roughly how long a normal spoken reply should run, as a hint folded
    #: into the respond-turn prompt. Nothing truncates audio, so this is a
    #: request the model honors rather than a hard limit. 0 leaves length to
    #: the model's own judgement.
    reply_chars: int = Field(default=60, ge=0)

    #: How often a camera frame is pushed to the model. Frames are billed as
    #: input tokens and Realtime re-bills the whole context every turn, so
    #: this is the main cost dial after the model choice.
    frame_seconds: float = Field(default=10.0, gt=0)

    #: How long a session runs before ending on its own (``--seconds`` on the CLI).
    session_seconds: float = Field(default=120.0, gt=0)

    @field_validator("voice")
    @classmethod
    def _voice_must_be_known(cls, value: str) -> str:
        """Fail fast on a bad ``COMPANION_AGENT_VOICE`` instead of only failing once the API rejects it.

        ``--voice`` is already restricted to :data:`VOICES` by argparse's own
        ``choices=`` in :func:`~.app.main`, but that only covers the CLI flag
        -- an env-supplied value reaches here unchecked otherwise, since
        ``argparse`` never sees it at all (the flag's own default is
        ``None``, falling back to the settings value -- see ``main()``).
        """
        if value not in VOICES:
            raise ValueError(f"voice must be one of {VOICES}, got {value!r}")
        return value


def load_settings(**field_values: object) -> RealtimeSettings:
    """Build realtime settings off the project's own ``.env`` file (the normal entry point)."""
    return RealtimeSettings.with_env_file(_DEFAULT_ENV_FILE, **field_values)


__all__ = ["VOICES", "RealtimeSettings", "load_settings"]
