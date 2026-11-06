"""AI modes: the preset characters the pilot can switch to, and where their prompts come from.

A mode is an id, a display label, and a prompt. `PromptSource` is the seam
for where prompts live: `BundledPrompts` reads the presets shipped with this
app, and `EditablePrompts` layers the edits saved from the homepage on top of
them. A source that fetches edited prompts from Palmimo Portal can replace it
without the controller changing, because prompts are read at mode start,
never at app start, so an edit applies on the next switch.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


_PROMPT_DIR = Path(__file__).parent

#: Longest prompt `EditablePrompts.save` accepts. The shipped presets are well
#: under 1,000 characters; this only stops a runaway paste.
MAX_PROMPT_CHARS: int = 8000


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


def default_prompt_dir() -> Path:
    """Where homepage edits are saved: outside the package, so an app update keeps them.

    Follows the SDK's own state-directory convention
    (`palmimo_sdk._flight_recorder.default_incident_dir`), under the data
    home instead of the state home since prompts are user content.
    """
    data = os.environ.get("XDG_DATA_HOME", "")
    root = Path(data) if data and Path(data).is_absolute() else Path.home() / ".local" / "share"
    return root / "palmimo" / "controller" / "prompts"


class EditablePrompts:
    """The shipped presets, each replaceable by an edit saved in *directory*.

    The mode list stays the presets: only their prompts can change. A saved
    edit is one `<id>.md` file; deleting it brings the shipped text back.
    """

    def __init__(self, directory: Path, bundled: BundledPrompts | None = None) -> None:
        self._directory = directory
        self._bundled = bundled if bundled is not None else BundledPrompts()

    def modes(self) -> list[Mode]:
        return self._bundled.modes()

    def _check(self, mode_id: str) -> Path:
        if mode_id not in {mode.id for mode in self._bundled.modes()}:
            raise KeyError(mode_id)
        return self._directory / f"{mode_id}.md"

    def default_prompt(self, mode_id: str) -> str:
        """The shipped prompt for *mode_id*. Raises `KeyError` for an unknown mode."""
        return self._bundled.prompt(mode_id)

    def _saved(self, mode_id: str) -> str | None:
        try:
            text = self._check(mode_id).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return text or None

    def prompt(self, mode_id: str) -> str:
        saved = self._saved(mode_id)
        return saved if saved is not None else self.default_prompt(mode_id)

    def is_custom(self, mode_id: str) -> bool:
        """Whether *mode_id* runs a saved edit rather than the shipped prompt."""
        return self._saved(mode_id) is not None

    def save(self, mode_id: str, text: str) -> None:
        """Save *text* as *mode_id*'s prompt, replacing any earlier edit.

        Raises:
            KeyError: *mode_id* is not a preset.
            ValueError: *text* is blank or longer than `MAX_PROMPT_CHARS`.
            OSError: The prompt directory cannot be written.
        """
        path = self._check(mode_id)
        text = text.strip()
        if not text:
            raise ValueError("prompt is empty")
        if len(text) > MAX_PROMPT_CHARS:
            raise ValueError(f"prompt is longer than {MAX_PROMPT_CHARS} characters")
        self._directory.mkdir(parents=True, exist_ok=True)
        # Written beside the target and renamed over it, so a mode starting
        # mid-save reads either the old prompt or the new one, never half.
        fd, tmp = tempfile.mkstemp(dir=self._directory, prefix=f".{mode_id}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text + "\n")
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def reset(self, mode_id: str) -> None:
        """Drop *mode_id*'s saved edit, so the shipped prompt applies again. Raises `KeyError` for an unknown mode."""
        self._check(mode_id).unlink(missing_ok=True)


__all__ = [
    "MAX_PROMPT_CHARS",
    "PRESET_MODES",
    "BundledPrompts",
    "EditablePrompts",
    "Mode",
    "PromptSource",
    "default_prompt_dir",
]
