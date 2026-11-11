"""The Realtime voice AI modes speak in, chosen on the homepage and saved on the robot.

One voice for every mode. The Realtime API cannot change a session's voice
once it has spoken, so a change applies the next time an AI mode starts.
`VoicePreview` plays a bundled sample of each voice on the robot's speaker, so
a voice can be heard before it is chosen.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .ai.audio import API_RATE
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


#: Where the bundled samples live, one `<voice>.wav` each. Regenerate them with
#: `scripts/record_voice_samples.py`.
SAMPLE_DIR = Path(__file__).parent / "voice_samples"

#: Bytes per `Playback.write` call, about 0.1 s of audio.
_CHUNK_BYTES = API_RATE * 2 // 10


def read_sample(voice: str, sample_dir: Path = SAMPLE_DIR) -> bytes:
    """Return *voice*'s bundled sample as raw 16-bit mono PCM at `API_RATE`.

    Raises:
        ValueError: *voice* is not one of `VOICES`, or its file is not 16-bit mono at `API_RATE`.
        FileNotFoundError: The sample was never generated.
    """
    if voice not in VOICES:
        raise ValueError(f"unknown voice {voice!r}")
    with wave.open(str(sample_dir / f"{voice}.wav"), "rb") as clip:
        shape = (clip.getnchannels(), clip.getsampwidth(), clip.getframerate())
        if shape != (1, 2, API_RATE):
            raise ValueError(f"{voice}.wav is {shape}, expected (1, 2, {API_RATE})")
        return clip.readframes(clip.getnframes())


class PreviewPlaybackLike(Protocol):
    """The part of `ai.audio.Playback` a preview uses."""

    def write(self, pcm: bytes) -> None: ...

    def interrupt(self) -> None: ...

    def speaking(self, *, tail: float = 0.0) -> bool: ...

    def close(self) -> None: ...


class VoicePreview:
    """Plays one voice sample at a time on the robot's speaker.

    Runs on the server's event loop. A new preview cuts off the one playing,
    and `stop` must run before an AI mode opens the speaker.
    """

    def __init__(
        self,
        playback_factory: Callable[[], PreviewPlaybackLike],
        *,
        sample_dir: Path = SAMPLE_DIR,
        poll_s: float = 0.1,
    ) -> None:
        self._playback_factory = playback_factory
        self._sample_dir = sample_dir
        self._poll_s = poll_s
        self._lock = asyncio.Lock()
        self._playback: PreviewPlaybackLike | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def playing(self) -> bool:
        return self._task is not None and not self._task.done()

    async def play(self, voice: str) -> float:
        """Start playing *voice*'s sample, cutting off any preview already playing.

        Returns:
            float: The sample's length in seconds.

        Raises:
            ValueError: *voice* is unknown or its sample is malformed.
            FileNotFoundError: The sample was never generated.
        """
        pcm = read_sample(voice, self._sample_dir)
        async with self._lock:
            await self._stop()
            # Resolving the output device lists ALSA cards, which blocks.
            playback = await asyncio.to_thread(self._playback_factory)
            for start in range(0, len(pcm), _CHUNK_BYTES):
                playback.write(pcm[start : start + _CHUNK_BYTES])
            self._playback = playback
            self._task = asyncio.create_task(self._finish(playback), name=f"voice-preview-{voice}")
        return len(pcm) / (API_RATE * 2)

    async def stop(self) -> None:
        """Cut off the preview playing, if any. Safe to call at any time."""
        async with self._lock:
            await self._stop()

    async def _stop(self) -> None:
        task, self._task = self._task, None
        playback, self._playback = self._playback, None
        if playback is not None:
            playback.interrupt()
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # Closed here, not by the cancelled task: a task cancelled before its
        # first step never runs its body at all.
        if playback is not None:
            await asyncio.to_thread(playback.close)

    async def _finish(self, playback: PreviewPlaybackLike) -> None:
        # `close` discards audio still queued, so it waits for the sample to finish.
        while playback.speaking():
            await asyncio.sleep(self._poll_s)
        # A `_stop` that already took this playback closes it instead.
        if self._playback is playback:
            self._playback = None
            await asyncio.to_thread(playback.close)


__all__ = [
    "DEFAULT_VOICE",
    "SAMPLE_DIR",
    "VOICES",
    "PreviewPlaybackLike",
    "VoicePreview",
    "VoiceSetting",
    "default_voice_path",
    "read_sample",
]
