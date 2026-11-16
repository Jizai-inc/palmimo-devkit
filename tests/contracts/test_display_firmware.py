"""The shipped firmware declaration must match the serial client's requirement."""

import json
import subprocess
from importlib.resources import files
from pathlib import Path
from zipfile import ZipFile


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
    installed = json.loads(files("palmimo_sdk").joinpath("display_firmware.json").read_text())
    assert declaration == installed == {"schema": 1, "protocol": 2}
