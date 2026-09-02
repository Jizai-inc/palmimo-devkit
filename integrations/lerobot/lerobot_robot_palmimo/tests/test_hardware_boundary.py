"""The integration plugins open no hardware backend of their own.

AGENTS.md already says it — "Anything built on the SDK delegates gait to
MotionEngine and never opens a hardware backend of its own" — and the plugin
opened its own Dynamixel bus anyway for long enough that the two startup and
shutdown behaviors diverged. Prose did not hold the line, so this does.

Scope is the importable plugin code: the classes LeRobot instantiates and
everything they pull in. ``scripts/`` is excluded on purpose — those are
standalone recovery CLIs that must keep working when only part of the robot
answers the bus, the same carve-out ``scripts/diagnose_servos.py`` has in the
SDK tree. They are not on the robot path, and nothing on the robot path imports
them.
"""

from __future__ import annotations

import ast
from pathlib import Path


# Serial transports and the bus classes built on them. ``lerobot.motors`` is
# allowed only where the plugin translates Palmimo's layout into LeRobot's
# ``Motor`` objects (motor_layout.py); driving them is what is banned.
_HARDWARE_BACKENDS = frozenset(
    {
        "dynamixel_sdk",
        "serial",
        "lerobot.motors.dynamixel",
        "lerobot.motors.motors_bus",
        "lerobot.motors.feetech",
    }
)

# The one module allowed to name a bus class: it copies XC330-M288 into LeRobot's
# Dynamixel model tables, which means reaching for DynamixelMotorsBus as a table
# holder. It never constructs one; other tooling imports it for that layout.
_LAYOUT_MODULE = "motor_layout.py"

_PLUGIN_PACKAGES = (
    "lerobot_robot_palmimo/lerobot_robot_palmimo",
    "lerobot_teleoperator_palmimo/lerobot_teleoperator_palmimo",
)

_INTEGRATION_ROOT = Path(__file__).resolve().parents[2]


def _plugin_modules() -> list[Path]:
    modules = []
    for package in _PLUGIN_PACKAGES:
        for path in sorted((_INTEGRATION_ROOT / package).rglob("*.py")):
            if "scripts" in path.relative_to(_INTEGRATION_ROOT).parts:
                continue
            modules.append(path)
    return modules


def _imported_modules(source: str) -> set[str]:
    """Every module named by an import in *source*, including lazy ones."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_the_plugin_tree_is_not_empty() -> None:
    """A path typo would otherwise make every check below pass vacuously."""
    modules = _plugin_modules()

    assert len(modules) >= 6
    assert any(path.name == "palmimo.py" for path in modules)


def test_no_plugin_module_imports_a_hardware_backend() -> None:
    offenders = {}
    for path in _plugin_modules():
        if path.name == _LAYOUT_MODULE:
            continue
        banned = sorted(_imported_modules(path.read_text(encoding="utf-8")) & _HARDWARE_BACKENDS)
        if banned:
            offenders[path.name] = banned

    assert not offenders, f"plugin modules must reach hardware through palmimo_sdk: {offenders}"


def test_the_layout_module_names_a_bus_but_never_builds_one() -> None:
    """The carve-out is a model-table lookup, not a bus the plugin can drive."""
    source = (_INTEGRATION_ROOT / _PLUGIN_PACKAGES[0] / _LAYOUT_MODULE).read_text(encoding="utf-8")

    constructed = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "DynamixelMotorsBus" not in constructed


def test_the_robot_plugin_reaches_hardware_through_the_sdk() -> None:
    """The positive half: banning a bus only helps if the SDK took its place."""
    source = (_INTEGRATION_ROOT / _PLUGIN_PACKAGES[0] / "palmimo.py").read_text(encoding="utf-8")

    imported = _imported_modules(source)

    assert "palmimo_sdk.DynamixelDriver" in imported
    assert "palmimo_sdk.Palmimo" in imported
