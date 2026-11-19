"""Behavior of `palmimo_controller.modes.BundledPrompts` and `EditablePrompts`."""

from pathlib import Path

import pytest

from palmimo_controller.modes import (
    MAX_PROMPT_CHARS,
    PRESET_MODES,
    BundledPrompts,
    EditablePrompts,
    ModeSetting,
    default_mode_path,
    default_prompt_dir,
)


def test_every_preset_mode_has_a_nonempty_prompt() -> None:
    prompts = BundledPrompts()
    for mode in PRESET_MODES:
        assert prompts.prompt(mode.id)


def test_presets_are_default_kansai_then_custom() -> None:
    assert [mode.id for mode in BundledPrompts().modes()] == ["default", "kansai", "custom"]


def test_prompt_rejects_an_unknown_mode() -> None:
    # Without this, a crafted mode id could read any file next to the prompts.
    with pytest.raises(KeyError):
        BundledPrompts().prompt("../__init__")


def test_editable_prompts_fall_back_to_the_bundled_text(tmp_path: Path) -> None:
    prompts = EditablePrompts(tmp_path / "missing")
    for mode in PRESET_MODES:
        assert prompts.prompt(mode.id) == BundledPrompts().prompt(mode.id)
        assert prompts.is_custom(mode.id) is False


def test_saved_prompt_wins_until_reset(tmp_path: Path) -> None:
    prompts = EditablePrompts(tmp_path)
    prompts.save("kansai", "  custom kansai  ")
    assert prompts.prompt("kansai") == "custom kansai"
    assert prompts.is_custom("kansai") is True
    assert prompts.prompt("default") == BundledPrompts().prompt("default")

    prompts.reset("kansai")
    assert prompts.prompt("kansai") == BundledPrompts().prompt("kansai")
    assert prompts.is_custom("kansai") is False
    prompts.reset("kansai")  # resetting an unedited prompt is harmless


def test_save_writes_only_the_mode_file_and_leaves_the_bundled_preset(tmp_path: Path) -> None:
    # Without this, an edit could overwrite the shipped preset that reset restores.
    bundled_before = BundledPrompts().prompt("kansai")
    EditablePrompts(tmp_path).save("kansai", "custom")
    assert [path.name for path in tmp_path.iterdir()] == ["kansai.md"]
    assert BundledPrompts().prompt("kansai") == bundled_before


def test_a_blank_saved_file_falls_back_to_the_bundled_text(tmp_path: Path) -> None:
    (tmp_path / "kansai.md").write_text("  \n", encoding="utf-8")
    assert EditablePrompts(tmp_path).prompt("kansai") == BundledPrompts().prompt("kansai")


@pytest.mark.parametrize("text", ["", "  \n ", "x" * (MAX_PROMPT_CHARS + 1)])
def test_save_rejects_blank_or_too_long_text(tmp_path: Path, text: str) -> None:
    with pytest.raises(ValueError):
        EditablePrompts(tmp_path).save("kansai", text)
    assert not (tmp_path / "kansai.md").exists()


@pytest.mark.parametrize("mode_id", ["bogus", "../kansai", "../../etc/passwd"])
def test_editable_prompts_reject_an_unknown_mode(tmp_path: Path, mode_id: str) -> None:
    # Without this, a crafted mode id could read, write or delete any file.
    prompts = EditablePrompts(tmp_path / "prompts")
    with pytest.raises(KeyError):
        prompts.prompt(mode_id)
    with pytest.raises(KeyError):
        prompts.save(mode_id, "x")
    with pytest.raises(KeyError):
        prompts.reset(mode_id)
    assert not (tmp_path / "prompts").exists()


def test_default_prompt_dir_follows_xdg_data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_prompt_dir() == tmp_path / "palmimo" / "controller" / "prompts"
    monkeypatch.setenv("XDG_DATA_HOME", "relative/path")
    assert default_prompt_dir() == Path.home() / ".local" / "share" / "palmimo" / "controller" / "prompts"


def test_mode_setting_defaults_to_the_first_preset_until_one_is_saved(tmp_path: Path) -> None:
    setting = ModeSetting(tmp_path / "mode.txt", BundledPrompts())
    assert (setting.get(), setting.label()) == ("default", "標準")
    setting.save("kansai")
    assert (setting.get(), setting.label()) == ("kansai", "関西弁")


def test_mode_setting_rejects_an_unknown_mode(tmp_path: Path) -> None:
    setting = ModeSetting(tmp_path / "mode.txt", BundledPrompts())
    with pytest.raises(ValueError):
        setting.save("../evil")
    assert not (tmp_path / "mode.txt").exists()


def test_a_hand_edited_unknown_mode_falls_back_to_the_default(tmp_path: Path) -> None:
    # Without this, a typo in the saved file would leave the AI button with nothing to start.
    path = tmp_path / "mode.txt"
    path.write_text("nobody\n", encoding="utf-8")
    assert ModeSetting(path, BundledPrompts()).get() == "default"


def test_mode_label_passes_an_unknown_id_through(tmp_path: Path) -> None:
    assert ModeSetting(tmp_path / "mode.txt", BundledPrompts()).label("gone") == "gone"


def test_default_mode_path_sits_beside_the_prompt_edits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_mode_path() == tmp_path / "palmimo" / "controller" / "mode.txt"
