from pathlib import Path

import pytest

from palmimo_controller.voices import DEFAULT_VOICE, VOICES, VoiceSetting, default_voice_path


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
