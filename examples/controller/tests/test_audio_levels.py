"""Behavior of `palmimo_controller.audio_levels`: the saved speaker/mic levels and the mic meter's peak hold."""

from pathlib import Path

import pytest

from palmimo_controller.audio_levels import (
    DEFAULT_MIC_GAIN,
    DEFAULT_SPEAKER_VOLUME,
    AudioLevels,
    Levels,
    MicLevel,
)


def test_levels_start_at_the_defaults_when_nothing_is_saved(tmp_path: Path) -> None:
    levels = AudioLevels(tmp_path / "audio.json")
    assert levels.get() == Levels(DEFAULT_SPEAKER_VOLUME, DEFAULT_MIC_GAIN)
    assert levels.speaker_gain() == 1.0
    assert levels.mic_gain() == 1.0


def test_saved_levels_apply_at_once_and_survive_a_restart(tmp_path: Path) -> None:
    # Without this, a slider moved on the homepage would not reach the running
    # AI mode, or would be forgotten when the app restarts.
    path = tmp_path / "settings" / "audio.json"
    levels = AudioLevels(path)
    levels.save(speaker_volume=40, mic_gain=150)
    assert levels.speaker_gain() == pytest.approx(0.4)
    assert levels.mic_gain() == pytest.approx(1.5)
    assert AudioLevels(path).get() == Levels(40, 150)


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"speaker_volume": 500, "mic_gain": "loud"}'])
def test_a_broken_file_falls_back_to_the_defaults(tmp_path: Path, content: str) -> None:
    # Without this, a hand-edited file could stop every AI mode from starting.
    path = tmp_path / "audio.json"
    path.write_text(content, encoding="utf-8")
    assert AudioLevels(path).get() == Levels()


def test_each_saved_value_falls_back_on_its_own(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    path.write_text('{"speaker_volume": 30, "mic_gain": 999}', encoding="utf-8")
    assert AudioLevels(path).get() == Levels(30, DEFAULT_MIC_GAIN)


@pytest.mark.parametrize(("speaker", "mic"), [(-1, 100), (101, 100), (50, -5), (50, 201)])
def test_save_rejects_out_of_range_levels_and_keeps_the_old_ones(tmp_path: Path, speaker: int, mic: int) -> None:
    path = tmp_path / "audio.json"
    levels = AudioLevels(path)
    with pytest.raises(ValueError):
        levels.save(speaker_volume=speaker, mic_gain=mic)
    assert levels.get() == Levels()
    assert not path.exists()


def test_mic_level_is_none_until_the_mic_reports() -> None:
    assert MicLevel().take() is None


def test_mic_level_holds_the_peak_until_taken() -> None:
    # Without this, a meter polling slower than chunks arrive would miss a
    # short loud word between two quiet chunks.
    level = MicLevel()
    level.report(0.2)
    level.report(0.9)
    level.report(0.1)
    assert level.take() == 0.9
    assert level.take() == 0.0


def test_cleared_mic_level_reads_as_closed() -> None:
    level = MicLevel()
    level.report(0.5)
    level.clear()
    assert level.take() is None
