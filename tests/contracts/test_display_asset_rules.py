"""The standalone manifest validator and SDK must accept the same asset names."""

import ast
from pathlib import Path

from tools.manifest import ASSET_NAME_RE, ASSET_SLOT_RE


def test_manifest_asset_rules_match_sdk() -> None:
    root = Path(__file__).resolve().parents[2] / "packages/palmimo_sdk/palmimo_sdk"
    for path, constant, manifest_rule in (
        (root / "_display_names.py", "ASSET_NAME_RE", ASSET_NAME_RE),
        (root / "io/display.py", "_SLOT_ID", ASSET_SLOT_RE),
    ):
        tree = ast.parse(path.read_text())
        assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == constant for target in node.targets)
        )
        assert isinstance(assignment.value, ast.Call)
        sdk_rule = ast.literal_eval(assignment.value.args[0])
        assert sdk_rule == manifest_rule.pattern.removeprefix("^").removesuffix("$")
