"""The Realtime voice AI modes speak in, chosen on the homepage and saved on the robot.

One voice for every mode. The Realtime API cannot change a session's voice
once it has spoken, so a change applies the next time an AI mode starts.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .modes import default_prompt_dir


#: The Realtime API's built-in voices.
VOICES: tuple[str, ...] = ("alloy", "ash", "ballad", "cedar", "coral", "echo", "marin", "sage", "shimmer", "verse")
DEFAULT_VOICE = "coral"


def default_voice_path() -> Path:
    """Where the homepage's choice is saved: beside the prompt edits, so an app update keeps it."""
    return default_prompt_dir().parent / "voice.txt"


class VoiceSetting:
    """The saved voice, or `DEFAULT_VOICE` when none is saved."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _saved(self) -> str | None:
        try:
            voice = self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        # A file edited by hand into something unknown falls back rather than
        # breaking every AI mode start.
        return voice if voice in VOICES else None

    def get(self) -> str:
        saved = self._saved()
        return saved if saved is not None else DEFAULT_VOICE

    def is_custom(self) -> bool:
        """Whether a voice other than the default is saved."""
        return self._saved() is not None

    def save(self, voice: str) -> None:
        """Save *voice* as the AI modes' voice.

        Raises:
            ValueError: *voice* is not one of `VOICES`.
            OSError: The settings directory cannot be written.
        """
        if voice not in VOICES:
            raise ValueError(f"unknown voice {voice!r}")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Written beside the target and renamed over it, so a mode starting
        # mid-save reads either the old voice or the new one.
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".voice.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(voice + "\n")
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


__all__ = ["DEFAULT_VOICE", "VOICES", "VoiceSetting", "default_voice_path"]
