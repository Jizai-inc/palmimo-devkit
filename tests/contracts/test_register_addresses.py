"""Register addresses are stated twice in this tree, and must agree.

The SDK reaches the servos through `DynamixelBus.CONTROL_TABLE`; the diagnostic
script talks to them directly through its own `ADDR_*` constants, on purpose —
it has to keep working when the SDK path is what's broken. That independence is
worth the duplication, but not the failure mode it invites: one table corrected
and the other left behind, with the script quietly reading the wrong register.

This contract does not merge the two. It only holds them to the same numbers.
Both tables are read from source rather than imported: the addresses are plain
literals, the SDK's table is internal to `palmimo_sdk.io`, and importing the
script would pull in `dynamixel_sdk` and its argument parsing.
"""

import ast
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DIAGNOSE_SCRIPT = REPO_ROOT / "scripts" / "diagnose_servos.py"
BUS_MODULE = REPO_ROOT / "packages" / "palmimo_sdk" / "palmimo_sdk" / "io" / "_dynamixel_bus.py"

# Diagnostic `ADDR_*` constant -> the register it addresses in CONTROL_TABLE.
# A register the script does not read has no entry here; teaching the script a
# new one means adding it here too.
ADDRESS_CONSTANTS = {
    "ADDR_OPERATING_MODE": "Operating_Mode",
    "ADDR_TORQUE_ENABLE": "Torque_Enable",
    "ADDR_POSITION_P_GAIN": "Position_P_Gain",
    "ADDR_PROFILE_ACCELERATION": "Profile_Acceleration",
    "ADDR_PROFILE_VELOCITY": "Profile_Velocity",
    "ADDR_GOAL_POSITION": "Goal_Position",
    "ADDR_PRESENT_CURRENT": "Present_Current",
    "ADDR_PRESENT_POSITION": "Present_Position",
    "ADDR_PRESENT_VOLTAGE": "Present_Input_Voltage",
    "ADDR_PRESENT_TEMPERATURE": "Present_Temperature",
}


def _module_int_constants(path: Path) -> dict[str, int]:
    """Return the module-level ``NAME = <int>`` assignments in *path*."""
    assert path.is_file(), f"Expected source file is missing: {path}"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, int):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                found[target.id] = node.value.value
    return found


def _sdk_control_table() -> dict[str, tuple[int, int]]:
    """Return the SDK's ``CONTROL_TABLE`` as register name -> (address, size)."""
    assert BUS_MODULE.is_file(), f"Bus module is missing: {BUS_MODULE}"
    tree = ast.parse(BUS_MODULE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            target: ast.expr | None = node.target
        elif isinstance(node, ast.Assign) and node.targets:
            target = node.targets[0]
        else:
            continue
        if not isinstance(target, ast.Name) or target.id != "CONTROL_TABLE":
            continue
        literal = node.value
        assert isinstance(literal, ast.Dict), "CONTROL_TABLE must be a dict literal."
        table: dict[str, tuple[int, int]] = {}
        for key, value in zip(literal.keys, literal.values, strict=True):
            assert isinstance(key, ast.Constant) and isinstance(key.value, str), "CONTROL_TABLE keys must be literals."
            assert isinstance(value, ast.Tuple), "CONTROL_TABLE values must be (address, size) literals."
            address, size = value.elts[0], value.elts[1]
            assert isinstance(address, ast.Constant) and isinstance(address.value, int)
            assert isinstance(size, ast.Constant) and isinstance(size.value, int)
            table[key.value] = (address.value, size.value)
        return table
    raise AssertionError(f"No CONTROL_TABLE literal found in {BUS_MODULE}.")


def test_diagnostic_script_addresses_match_the_sdk_control_table() -> None:
    control_table = _sdk_control_table()
    constants = _module_int_constants(DIAGNOSE_SCRIPT)
    known = {name: value for name, value in constants.items() if name in ADDRESS_CONSTANTS}
    assert known, "No ADDR_* constants were found in the diagnostic script; the contract would pass vacuously."

    mismatched = {
        name: (value, control_table[ADDRESS_CONSTANTS[name]][0])
        for name, value in known.items()
        if value != control_table[ADDRESS_CONSTANTS[name]][0]
    }
    assert not mismatched, (
        "The diagnostic script and the SDK disagree on register addresses "
        f"(constant: script vs SDK): {mismatched}. Correct both tables together."
    )


def test_every_mapped_address_constant_still_exists_in_the_script() -> None:
    """A renamed or dropped constant must not silently stop being checked."""
    constants = _module_int_constants(DIAGNOSE_SCRIPT)
    missing = sorted(name for name in ADDRESS_CONSTANTS if name not in constants)
    assert not missing, (
        f"These constants are mapped here but no longer in {DIAGNOSE_SCRIPT.name}: {missing}. "
        "Update ADDRESS_CONSTANTS so the check keeps covering what the script reads."
    )


def test_every_mapped_register_still_exists_in_the_sdk_table() -> None:
    """The same, from the SDK side: a renamed register must not go unchecked."""
    control_table = _sdk_control_table()
    missing = sorted(set(ADDRESS_CONSTANTS.values()) - set(control_table))
    assert not missing, (
        f"These registers are mapped here but no longer in CONTROL_TABLE: {missing}. "
        "Update ADDRESS_CONSTANTS to match the SDK's register names."
    )


def _width_specific_calls() -> list[tuple[str, int]]:
    """Return ``(ADDR_* name, byte width)`` for each width-specific register call.

    The script reads and writes through `read2ByteTxRx` / `write1ByteTxRx` and
    friends, where the width is baked into the method name and the register is
    an `ADDR_*` argument. A width that disagrees with the register's real size
    corrupts the value silently, which is harder to spot than a wrong address.
    """
    tree = ast.parse(DIAGNOSE_SCRIPT.read_text(encoding="utf-8"))
    calls: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        match = re.fullmatch(r"(?:read|write)([124])ByteTxRx", node.func.attr)
        if match is None:
            continue
        for argument in node.args:
            if isinstance(argument, ast.Name) and argument.id in ADDRESS_CONSTANTS:
                calls.append((argument.id, int(match.group(1))))
    return calls


def test_diagnostic_script_reads_each_register_at_its_declared_width() -> None:
    control_table = _sdk_control_table()
    calls = _width_specific_calls()
    assert calls, "No width-specific register calls were found; the contract would pass vacuously."

    mismatched = sorted(
        {
            (name, width, control_table[ADDRESS_CONSTANTS[name]][1])
            for name, width in calls
            if width != control_table[ADDRESS_CONSTANTS[name]][1]
        }
    )
    assert not mismatched, (
        "The diagnostic script accesses registers at a width the SDK does not declare "
        f"(constant, script width, SDK size): {mismatched}."
    )
