from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_incident_state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    # Prompt edits saved by a test land in its own tmp dir, never the real home.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
