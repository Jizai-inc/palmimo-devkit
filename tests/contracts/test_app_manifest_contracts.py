"""Every app example must ship a `palmimo.toml` -- Portal's catalog and installer read only that file.

Scoped the same way tests/contracts/test_layering_contracts.py identifies an
example project: a directory directly under examples/ or examples/agents/
that carries its own pyproject.toml. openclaw is a Docker-based connection
kit with no palmimo_sdk app to launch (see its README), so it is excluded the
same way the root uv workspace excludes it from `[tool.uv.workspace]`.
"""

from collections import Counter
from pathlib import Path

from tools.build_catalog import discover_manifests
from tools.manifest import load_manifest


SOFTWARE_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = SOFTWARE_ROOT / "examples"
EXCLUDED_APP_DIRS = frozenset({"examples/agents/openclaw"})


def _app_example_dirs() -> list[Path]:
    candidates = list(EXAMPLES_DIR.glob("*")) + list((EXAMPLES_DIR / "agents").glob("*"))
    return sorted(
        path
        for path in candidates
        if path.is_dir()
        and (path / "pyproject.toml").is_file()
        and path.relative_to(SOFTWARE_ROOT).as_posix() not in EXCLUDED_APP_DIRS
    )


def test_every_app_example_has_a_palmimo_manifest() -> None:
    app_dirs = _app_example_dirs()
    assert app_dirs, f"no app example directories found under {EXAMPLES_DIR}"
    missing = [path.relative_to(SOFTWARE_ROOT).as_posix() for path in app_dirs if not (path / "palmimo.toml").is_file()]
    assert missing == [], f"these app examples ship no palmimo.toml: {missing}"


def test_every_manifest_name_is_unique_across_examples() -> None:
    # A collision silently drops one app from the release catalog
    # (tools/build_catalog.py keys entries by name) with no other signal.
    names = [load_manifest(path).name for path in discover_manifests(SOFTWARE_ROOT)]
    duplicates = [name for name, count in Counter(names).items() if count > 1]
    assert duplicates == [], f"duplicate app manifest name(s): {duplicates}"
