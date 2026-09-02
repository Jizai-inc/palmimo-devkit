"""Every layering invariant the two-runtime split is built on.

The package README's "Layout: one character, two runtimes" section
(README.md#layout-one-character-two-runtimes) states these as guarantees,
and ``core/``'s own modules (see e.g. ``reflexes.py``'s docstring) repeat the
``core/`` half as a hard constraint:

- ``core/`` is the shared character layer -- prompts, tools, toolview,
  vision, reflexes. It imports nothing from either runtime: not
  ``pipeline/`` (the cascaded STT -> LLM -> TTS runtime) and not
  ``realtime/`` (the OpenAI Realtime API runtime). This is what makes the
  split worth having -- a second runtime can reuse the character without
  dragging the first runtime in.
- ``realtime/`` never imports from ``pipeline/`` and ``pipeline/`` never
  imports from ``realtime/`` -- the two runtimes are siblings, not layered
  on each other.
- The reverse is fine by design and deliberately NOT pinned here:
  ``pipeline/`` MAY import ``core/``, and ``realtime/`` MAY import ``core/``
  -- that is exactly how each runtime reuses the shared character.

AGENTS.md's own rule is that deterministic, judged behavior belongs in
pytest rather than being left for a reviewer to notice by hand -- this is
exactly that kind of rule, so it gets a test instead of staying a claim in
prose. All of it lives in one file (rather than split per source directory)
so the full contract set stays visible in one place instead of scattered
across half-pins.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "palmimo_companion_agent"
CORE_ROOT = PACKAGE_ROOT / "core"
PIPELINE_ROOT = PACKAGE_ROOT / "pipeline"
REALTIME_ROOT = PACKAGE_ROOT / "realtime"


def _sources(root: Path) -> list[Path]:
    sources = sorted(root.rglob("*.py"))
    assert sources, f"No sources found under {root}"
    return sources


def _absolute_imports(tree: ast.AST) -> set[str]:
    """Full dotted module paths of absolute imports (mirrors tests/contracts/test_layering_contracts.py)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _relative_targets(tree: ast.AST) -> set[str]:
    """First segment of each relative import target (``from ..pipeline import x`` -> ``pipeline``).

    Level (how many dots) doesn't change what the first named segment is, so
    checking it alone -- the same approach test_layering_contracts.py uses
    for the ``palmimo_sdk.io`` boundary -- is enough: a relative import
    reaching the forbidden package always names it as its first segment,
    from wherever in the source tree it is written.
    """
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            if node.module:
                targets.add(node.module.split(".")[0])
            else:
                targets.update(alias.name.split(".")[0] for alias in node.names)
    return targets


def _reaches(tree: ast.AST, forbidden: str) -> bool:
    """Whether *tree* imports the ``palmimo_companion_agent.<forbidden>`` package, absolute or relative."""
    absolute_hit = any(
        name == f"palmimo_companion_agent.{forbidden}" or name.startswith(f"palmimo_companion_agent.{forbidden}.")
        for name in _absolute_imports(tree)
    )
    relative_hit = forbidden in _relative_targets(tree)
    return absolute_hit or relative_hit


def _offenders(root: Path, forbidden: str) -> list[str]:
    return [
        path.relative_to(root).as_posix()
        for path in _sources(root)
        if _reaches(ast.parse(path.read_text(encoding="utf-8")), forbidden)
    ]


def test_no_module_under_core_imports_from_pipeline() -> None:
    offenders = _offenders(CORE_ROOT, "pipeline")
    assert offenders == [], f"core/ must never import from pipeline/: {offenders}"


def test_no_module_under_core_imports_from_realtime() -> None:
    offenders = _offenders(CORE_ROOT, "realtime")
    assert offenders == [], f"core/ must never import from realtime/: {offenders}"


def test_no_module_under_realtime_imports_from_pipeline() -> None:
    offenders = _offenders(REALTIME_ROOT, "pipeline")
    assert offenders == [], f"realtime/ must never import from pipeline/: {offenders}"


def test_no_module_under_pipeline_imports_from_realtime() -> None:
    offenders = _offenders(PIPELINE_ROOT, "realtime")
    assert offenders == [], f"pipeline/ must never import from realtime/: {offenders}"


@pytest.mark.parametrize("forbidden", ["pipeline", "realtime"])
def test_absolute_import_is_caught(forbidden: str) -> None:
    tree = ast.parse(f"from palmimo_companion_agent.{forbidden}.settings import Settings\n")
    assert _reaches(tree, forbidden) is True


@pytest.mark.parametrize("forbidden", ["pipeline", "realtime"])
def test_relative_import_is_caught_regardless_of_depth(forbidden: str) -> None:
    assert _reaches(ast.parse(f"from ..{forbidden}.settings import Settings\n"), forbidden) is True
    assert _reaches(ast.parse(f"from ...{forbidden} import settings\n"), forbidden) is True
    assert _reaches(ast.parse(f"from .. import {forbidden}\n"), forbidden) is True


@pytest.mark.parametrize("forbidden", ["pipeline", "realtime"])
def test_a_module_named_the_same_elsewhere_is_not_flagged(forbidden: str) -> None:
    """Only the palmimo_companion_agent.<forbidden> package is forbidden, not any same-named import."""
    tree = ast.parse(f"import {forbidden}\nfrom some_other_lib.{forbidden} import Thing\n")
    assert _reaches(tree, forbidden) is False


@pytest.mark.parametrize("forbidden", ["pipeline", "realtime"])
def test_parent_package_from_import_is_caught(forbidden: str) -> None:
    """Regression: ``from palmimo_companion_agent import pipeline`` names the forbidden package only
    via the ``f"{module}.{alias}"`` combination _absolute_imports builds, not the bare ``module``."""
    tree = ast.parse(f"from palmimo_companion_agent import {forbidden}\n")
    assert _reaches(tree, forbidden) is True


@pytest.mark.parametrize("forbidden", ["pipeline", "realtime"])
def test_import_inside_type_checking_guard_is_caught(forbidden: str) -> None:
    """ast.walk descends into ``if TYPE_CHECKING:`` bodies, so a type-only import still counts as
    reaching the forbidden package -- this repo was burned once by a TYPE_CHECKING-only leak, so a
    detector that stopped at top-level statements would silently miss the same class of violation."""
    tree = ast.parse(
        f"from typing import TYPE_CHECKING\n"
        f"if TYPE_CHECKING:\n"
        f"    from palmimo_companion_agent.{forbidden}.settings import Settings\n"
    )
    assert _reaches(tree, forbidden) is True


def test_core_imports_are_not_flagged_as_reaching_pipeline_or_realtime() -> None:
    """core/ importing its own siblings must not trip either check."""
    tree = ast.parse("from .reflexes import ReflexEngine\nfrom .tools import COMPANION_TOOL_MODELS\n")
    assert _reaches(tree, "pipeline") is False
    assert _reaches(tree, "realtime") is False


def test_pipeline_importing_core_is_not_flagged() -> None:
    """The reverse direction is fine by design: pipeline/ (and realtime/) MAY import core/."""
    tree = ast.parse("from ...core.reflexes import ReflexEngine\nfrom ..client import RealtimeClientLike\n")
    assert _reaches(tree, "pipeline") is False
    assert _reaches(tree, "realtime") is False
