"""The shipped firmware declaration must match the serial client's requirement."""

import json
import subprocess
from pathlib import Path
from zipfile import ZipFile

from palmimo_sdk.io.display import DISPLAY_PROTOCOL


def test_wheel_declares_the_display_protocol_required_by_client(tmp_path: Path) -> None:
    """Without this, a missing declaration makes the platform flash incompatible firmware."""
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        ["uv", "build", "--package", "palmimo-sdk", "--wheel", "--out-dir", str(tmp_path)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel,) = tmp_path.glob("*.whl")
    with ZipFile(wheel) as archive:
        declaration = json.loads(archive.read("palmimo_sdk/display_firmware.json"))
    assert declaration == {"schema": 1, "protocol": DISPLAY_PROTOCOL}
    assert DISPLAY_PROTOCOL == 2
