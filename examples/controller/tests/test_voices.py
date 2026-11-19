import asyncio
import wave
from pathlib import Path

import pytest

from palmimo_controller.ai.audio import API_RATE
from palmimo_controller.voices import (
    DEFAULT_VOICE,
    SAMPLE_DIR,
    VOICES,
    VoicePreview,
    VoiceSetting,
    default_voice_path,
    read_sample,
)


def test_voice_defaults_until_one_is_saved(tmp_path: Path) -> None:
    voices = VoiceSetting(tmp_path / "voice.txt")
    assert (voices.get(), voices.is_custom()) == (DEFAULT_VOICE, False)
    voices.save("marin")
    assert (voices.get(), voices.is_custom()) == ("marin", True)


def test_voice_save_rejects_an_unknown_voice(tmp_path: Path) -> None:
    voices = VoiceSetting(tmp_path / "voice.txt")
    with pytest.raises(ValueError):
        voices.save("nobody")
    assert not (tmp_path / "voice.txt").exists()


def test_a_hand_edited_unknown_voice_falls_back_to_the_default(tmp_path: Path) -> None:
    # Without this, a typo in the saved file would break every AI mode start.
    path = tmp_path / "voice.txt"
    path.write_text("nobody\n", encoding="utf-8")
    assert VoiceSetting(path).get() == DEFAULT_VOICE


def test_default_voice_path_sits_beside_the_prompt_edits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_voice_path() == tmp_path / "palmimo" / "controller" / "voice.txt"


def test_the_default_voice_is_one_of_the_offered_voices() -> None:
    assert DEFAULT_VOICE in VOICES


def _write_sample(directory: Path, voice: str, *, frames: int = 4800, rate: int = API_RATE, channels: int = 1) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with wave.open(str(directory / f"{voice}.wav"), "wb") as clip:
        clip.setnchannels(channels)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(b"\x01\x00" * frames * channels)


class _FakePlayback:
    """Speaks for as many polls as `polls` says, then goes quiet."""

    def __init__(self, polls: int = 0) -> None:
        self.written = bytearray()
        self.polls = polls
        self.interrupted = False
        self.closed = False

    def write(self, pcm: bytes) -> None:
        self.written.extend(pcm)

    def interrupt(self) -> None:
        self.interrupted = True

    def speaking(self, *, tail: float = 0.0) -> bool:
        if self.interrupted or self.polls <= 0:
            return False
        self.polls -= 1
        return True

    def close(self) -> None:
        self.closed = True


def test_read_sample_returns_the_pcm_frames(tmp_path: Path) -> None:
    _write_sample(tmp_path, "marin", frames=10)
    assert read_sample("marin", tmp_path) == b"\x01\x00" * 10


def test_read_sample_rejects_an_unknown_voice(tmp_path: Path) -> None:
    # Without this, a crafted voice name could open any WAV next to the samples.
    with pytest.raises(ValueError):
        read_sample("../marin", tmp_path)


@pytest.mark.parametrize(("rate", "channels"), [(16000, 1), (API_RATE, 2)])
def test_read_sample_rejects_a_clip_in_the_wrong_format(tmp_path: Path, rate: int, channels: int) -> None:
    # Without this, a clip at the wrong rate would play at the wrong pitch.
    _write_sample(tmp_path, "marin", rate=rate, channels=channels)
    with pytest.raises(ValueError):
        read_sample("marin", tmp_path)


async def test_preview_plays_the_whole_sample_then_closes(tmp_path: Path) -> None:
    _write_sample(tmp_path, "marin")
    playback = _FakePlayback(polls=2)
    preview = VoicePreview(lambda: playback, sample_dir=tmp_path, poll_s=0)
    seconds = await preview.play("marin")
    assert seconds == pytest.approx(0.2)
    assert bytes(playback.written) == b"\x01\x00" * 4800
    for _ in range(200):
        if playback.closed:
            break
        await asyncio.sleep(0.005)
    assert playback.closed and not playback.interrupted and not preview.playing


async def test_a_new_preview_cuts_off_the_one_playing(tmp_path: Path) -> None:
    _write_sample(tmp_path, "marin")
    _write_sample(tmp_path, "cedar")
    playbacks = [_FakePlayback(polls=1000), _FakePlayback(polls=1000)]
    queue = iter(playbacks)
    preview = VoicePreview(lambda: next(queue), sample_dir=tmp_path, poll_s=0.001)
    await preview.play("marin")
    await preview.play("cedar")
    assert playbacks[0].interrupted and playbacks[0].closed
    assert not playbacks[1].interrupted
    await preview.stop()
    assert playbacks[1].interrupted and playbacks[1].closed and not preview.playing


async def test_preview_stop_is_safe_with_nothing_playing(tmp_path: Path) -> None:
    await VoicePreview(lambda: _FakePlayback(), sample_dir=tmp_path).stop()


async def test_preview_of_a_missing_sample_opens_no_speaker(tmp_path: Path) -> None:
    opened: list[_FakePlayback] = []

    def factory() -> _FakePlayback:
        opened.append(_FakePlayback())
        return opened[-1]

    preview = VoicePreview(factory, sample_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        await preview.play("marin")
    assert opened == []


def test_bundled_samples_cover_every_voice_in_the_api_format() -> None:
    # Without this, a voice added to VOICES would have a play button that only errors.
    if not any(SAMPLE_DIR.glob("*.wav")):
        pytest.skip("no samples recorded yet: run scripts/record_voice_samples.py")
    for voice in VOICES:
        assert read_sample(voice), voice
