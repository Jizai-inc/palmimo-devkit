"""Behavior of `palmimo_controller.modes.BundledPrompts`."""

import pytest

from palmimo_controller.modes import PRESET_MODES, BundledPrompts


def test_every_preset_mode_has_a_nonempty_prompt() -> None:
    prompts = BundledPrompts()
    for mode in PRESET_MODES:
        assert prompts.prompt(mode.id)


def test_presets_are_default_then_kansai() -> None:
    assert [mode.id for mode in BundledPrompts().modes()] == ["default", "kansai"]


def test_prompt_rejects_an_unknown_mode() -> None:
    # Without this, a crafted mode id could read any file next to the prompts.
    with pytest.raises(KeyError):
        BundledPrompts().prompt("../__init__")
