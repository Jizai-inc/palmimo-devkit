"""AI modes: the preset characters the pilot can switch to, and where their prompts come from.

A mode is an id, a display label, and a prompt. `PromptSource` is the seam
for where prompts live: `BundledPrompts` reads the presets shipped with this
app. A source that fetches edited prompts from Palmimo Portal can replace it
without the controller changing, because prompts are read at mode start,
never at app start, so an edit applies on the next switch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


_PROMPT_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Mode:
    """One AI mode the pilot can pick."""

    id: str
    label: str


#: Shipped presets, in the order the mode picker lists them.
PRESET_MODES: tuple[Mode, ...] = (
    Mode(id="default", label="標準"),
    Mode(id="kansai", label="関西弁"),
)


class PromptSource(Protocol):
    """Where the controller gets the mode list and each mode's prompt."""

    def modes(self) -> list[Mode]: ...

    def prompt(self, mode_id: str) -> str:
        """Return *mode_id*'s prompt. Raises `KeyError` for an unknown mode."""
        ...


class BundledPrompts:
    """The presets shipped in this package, one `<id>.md` file each."""

    def __init__(self, directory: Path = _PROMPT_DIR, modes: tuple[Mode, ...] = PRESET_MODES) -> None:
        self._directory = directory
        self._modes = modes

    def modes(self) -> list[Mode]:
        return list(self._modes)

    def prompt(self, mode_id: str) -> str:
        if mode_id not in {mode.id for mode in self._modes}:
            raise KeyError(mode_id)
        return (self._directory / f"{mode_id}.md").read_text(encoding="utf-8").strip()


__all__ = ["PRESET_MODES", "BundledPrompts", "Mode", "PromptSource"]
