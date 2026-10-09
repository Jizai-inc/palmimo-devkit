"""LeRobot discovers both plugins from their installed distribution names.

Runs in a fresh interpreter so modules another test already imported cannot
stand in for the discovery.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest


pytest.importorskip("lerobot")

_PROBE = """
import json
from lerobot.robots.config import RobotConfig
from lerobot.teleoperators.config import TeleoperatorConfig
from lerobot.utils.import_utils import register_third_party_plugins

register_third_party_plugins()
print(json.dumps({
    "robot": sorted(RobotConfig.get_known_choices()),
    "teleoperator": sorted(TeleoperatorConfig.get_known_choices()),
}))
"""


def test_third_party_plugin_discovery_registers_both_palmimo_plugins() -> None:
    result = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True, check=True)

    registered = json.loads(result.stdout.splitlines()[-1])
    assert "palmimo" in registered["robot"]
    assert "palmimo" in registered["teleoperator"]
