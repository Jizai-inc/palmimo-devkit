"""Layering contracts the docs state as guarantees.

architecture.md tells readers the core never depends on what builds on it and
that the engine performs no I/O. Both are claims a reader relies on — the SDK
ships on its own, so a single import in the wrong direction breaks it for anyone
who installs palmimo-sdk without the rest of the workspace.
"""

import ast
from pathlib import Path


SOFTWARE_ROOT = Path(__file__).resolve().parents[2]
SDK_PACKAGE = SOFTWARE_ROOT / "packages" / "palmimo_sdk" / "palmimo_sdk"
EXAMPLES_DIR = SOFTWARE_ROOT / "examples"


def _example_project_roots() -> list[Path]:
    """Directories under ``examples/`` that are their own uv project.

    A project root is identified by carrying a ``pyproject.toml`` directly,
    not by a fixed nesting depth — some examples sit straight under
    ``examples/`` (e.g. a hypothetical ``examples/foo/``), others are grouped
    one level deeper (``examples/agents/wakeword/``). Skips ``.venv/``, other
    hidden directories, and ``__pycache__`` so an installed or cached copy of
    a pyproject.toml never gets swept in.
    """
    return sorted(
        path.parent
        for path in EXAMPLES_DIR.rglob("pyproject.toml")
        if not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(EXAMPLES_DIR).parts)
    )


def _packages_built_on_the_sdk() -> tuple[str, ...]:
    """Module names of every example package that builds on the SDK.

    Each example project's importable package sits directly inside its own
    project root (the SDK core is the only package under ``packages/``); the
    SDK core must never import any of them.
    """
    names = [
        module.name
        for project in _example_project_roots()
        for module in project.iterdir()
        if module.is_dir() and (module / "__init__.py").exists()
    ]
    assert names, f"No example packages found under {EXAMPLES_DIR}"
    return tuple(sorted(names))


PACKAGES_BUILT_ON_THE_SDK = _packages_built_on_the_sdk()


def _sdk_sources() -> list[Path]:
    sources = sorted(SDK_PACKAGE.rglob("*.py"))
    assert sources, f"No SDK sources found under {SDK_PACKAGE}"
    return sources


def _absolute_imports(tree: ast.AST) -> set[str]:
    """Full dotted module paths of absolute imports (``import a.b`` / ``from a.b import c``).

    A ``from a.b import c`` also contributes ``a.b.c`` alongside ``a.b`` — the
    from-import alias form is otherwise indistinguishable from importing a
    plain attribute of ``a.b``, so without it ``from palmimo_sdk import io``
    would collapse to ``palmimo_sdk`` and slip past a check for the
    ``palmimo_sdk.io`` layer (the relative form, ``from . import io``, was
    already caught via its alias name).
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _relative_targets(tree: ast.AST) -> set[str]:
    """First segment of each relative import target (``from .io import x`` -> ``io``)."""
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            if node.module:
                targets.add(node.module.split(".")[0])
            else:
                targets.update(alias.name.split(".")[0] for alias in node.names)
    return targets


def _reaches_io(tree: ast.AST) -> list[str]:
    """Imports that reach the ``palmimo_sdk.io`` layer, absolute or relative.

    Any absolute hit is canonicalized to ``"palmimo_sdk.io"`` regardless of how
    deep the dotted path goes (``palmimo_sdk.io``, ``palmimo_sdk.io.dynamixel``,
    or the from-import alias form ``palmimo_sdk.io.ServoDriver`` all collapse to
    the same entry) — the contract cares only *whether* the io layer is
    reached, not which symbol or submodule was named.
    """
    absolute_hits = {
        "palmimo_sdk.io"
        for name in _absolute_imports(tree)
        if name == "palmimo_sdk.io" or name.startswith("palmimo_sdk.io.")
    }
    relative_hits = {name for name in _relative_targets(tree) if name == "io"}
    return sorted(absolute_hits | relative_hits)


def test_the_sdk_core_never_imports_a_package_built_on_it() -> None:
    offenders = [
        f"{path.relative_to(SDK_PACKAGE).as_posix()} -> {name}"
        for path in _sdk_sources()
        for name in sorted(_absolute_imports(ast.parse(path.read_text(encoding="utf-8"))))
        if name.split(".")[0] in PACKAGES_BUILT_ON_THE_SDK
    ]

    assert offenders == [], f"palmimo_sdk must not depend on packages built on it: {offenders}"


def test_the_motion_engine_never_imports_the_io_layer() -> None:
    engine = SDK_PACKAGE / "engine.py"
    tree = ast.parse(engine.read_text(encoding="utf-8"))
    reached = _reaches_io(tree)

    assert reached == [], f"engine.py computes only and must not reach the I/O layer: {reached}"


def test_absolute_io_import_is_still_caught_as_reaching_the_io_layer() -> None:
    """Regression: ``_absolute_imports`` used to truncate to the first dotted
    segment, so ``from palmimo_sdk.io import X`` collapsed to ``palmimo_sdk``
    and never matched the forbidden ``io`` layer. Full dotted paths catch it.
    """
    tree = ast.parse("from palmimo_sdk.io import ServoDriver\n")

    assert _reaches_io(tree) == ["palmimo_sdk.io"]


def test_stdlib_io_import_is_not_flagged_as_reaching_the_io_layer() -> None:
    """A stdlib ``import io`` (or any third-party ``foo.io.bar``) is not the
    forbidden ``palmimo_sdk.io`` layer and must not trip the contract.
    """
    tree = ast.parse("import io\nfrom foo.io.bar import x\n")

    assert _reaches_io(tree) == []


def test_from_import_alias_of_io_is_caught_as_reaching_the_io_layer() -> None:
    """Regression: ``from palmimo_sdk import io`` used to collapse to the
    absolute-import name ``palmimo_sdk`` (a package that is not itself
    forbidden), never contributing the ``palmimo_sdk.io`` the contract checks
    for. Recording the alias as ``module.alias`` catches this from-import form
    the same way the relative ``from . import io`` form was already caught.
    """
    tree = ast.parse("from palmimo_sdk import io\n")

    assert _reaches_io(tree) == ["palmimo_sdk.io"]
