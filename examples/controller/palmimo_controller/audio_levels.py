"""Speaker volume and microphone gain for the AI modes, set on the homepage and saved on the robot.

Both are software gain the controller applies to its own audio: the AI's
voice is scaled before it reaches ``aplay``, and the mic is scaled before it
reaches the Realtime API. A change applies to the next audio chunk, so a
slider moved mid-sentence takes effect at once. Speaker volume only
attenuates (software cannot go past the speaker's own maximum without
clipping); mic gain can also boost a quiet voice.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from .voices import default_voice_path


SPEAKER_VOLUME_MIN = 0
SPEAKER_VOLUME_MAX = 100
DEFAULT_SPEAKER_VOLUME = 100

MIC_GAIN_MIN = 0
MIC_GAIN_MAX = 200
DEFAULT_MIC_GAIN = 100


def default_audio_levels_path() -> Path:
    """Where the homepage's levels are saved: beside the voice choice, so an app update keeps them."""
    return default_voice_path().parent / "audio.json"


@dataclass(frozen=True)
class Levels:
    """Speaker volume and mic gain, both in percent."""

    speaker_volume: int = DEFAULT_SPEAKER_VOLUME
    mic_gain: int = DEFAULT_MIC_GAIN


def _valid_speaker(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and SPEAKER_VOLUME_MIN <= value <= SPEAKER_VOLUME_MAX


def _valid_mic(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and MIC_GAIN_MIN <= value <= MIC_GAIN_MAX


class AudioLevels:
    """The saved levels, held in memory so the audio path reads them per chunk without touching disk."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._levels = self._load()

    def _load(self) -> Levels:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return Levels()
        if not isinstance(raw, dict):
            return Levels()
        # Each value falls back on its own, so a file edited by hand into
        # something invalid never breaks an AI mode start.
        speaker = raw.get("speaker_volume")
        mic = raw.get("mic_gain")
        return Levels(
            speaker_volume=speaker if _valid_speaker(speaker) else DEFAULT_SPEAKER_VOLUME,
            mic_gain=mic if _valid_mic(mic) else DEFAULT_MIC_GAIN,
        )

    def get(self) -> Levels:
        return self._levels

    def speaker_gain(self) -> float:
        """Speaker volume as a multiplier for the AI's PCM."""
        return self._levels.speaker_volume / 100.0

    def mic_gain(self) -> float:
        """Mic gain as a multiplier for the captured PCM."""
        return self._levels.mic_gain / 100.0

    def save(self, *, speaker_volume: int, mic_gain: int) -> Levels:
        """Save both levels and use them from the next audio chunk.

        Raises:
            ValueError: A level is outside its range.
            OSError: The settings directory cannot be written.
        """
        if not _valid_speaker(speaker_volume):
            raise ValueError(
                f"speaker volume must be {SPEAKER_VOLUME_MIN}-{SPEAKER_VOLUME_MAX}, got {speaker_volume!r}"
            )
        if not _valid_mic(mic_gain):
            raise ValueError(f"mic gain must be {MIC_GAIN_MIN}-{MIC_GAIN_MAX}, got {mic_gain!r}")
        levels = Levels(speaker_volume=speaker_volume, mic_gain=mic_gain)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".audio.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"speaker_volume": speaker_volume, "mic_gain": mic_gain}, handle)
                handle.write("\n")
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        # Replaced only after the file is written, so a failed save leaves the
        # levels in use matching what is on disk.
        self._levels = levels
        return levels


class MicLevel:
    """The mic's loudest recent input, for the homepage meter.

    Holds the peak since the last `take()`, so a meter that polls slower than
    chunks arrive still shows a short loud word. `None` means the mic is not
    open. Written and read on the event loop only.
    """

    def __init__(self) -> None:
        self._peak: float | None = None

    def report(self, level: float) -> None:
        """Record one chunk's level, `0.0` (silent) to `1.0` (full scale)."""
        self._peak = level if self._peak is None else max(self._peak, level)

    def take(self) -> float | None:
        """Return the peak since the last call and start a new window, or `None` while the mic is closed."""
        peak = self._peak
        if peak is not None:
            self._peak = 0.0
        return peak

    def clear(self) -> None:
        """Mark the mic closed."""
        self._peak = None


__all__ = [
    "DEFAULT_MIC_GAIN",
    "DEFAULT_SPEAKER_VOLUME",
    "MIC_GAIN_MAX",
    "MIC_GAIN_MIN",
    "SPEAKER_VOLUME_MAX",
    "SPEAKER_VOLUME_MIN",
    "AudioLevels",
    "Levels",
    "MicLevel",
    "default_audio_levels_path",
]
