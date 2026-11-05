from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_incident_state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
