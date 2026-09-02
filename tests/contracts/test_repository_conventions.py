"""Placement and naming convention checks for this tree.

Every check here is scoped to the tree this test suite ships with: scan roots
stay inside it, so the suite runs unchanged wherever the tree lives.
"""

from pathlib import Path


SOFTWARE_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = SOFTWARE_ROOT / "scripts"

USER_SCRIPT_VERBS = {"diagnose"}


def _python_scripts(directory: Path) -> list[Path]:
    """Return the executable Python sources directly inside a scripts directory.

    Only direct children are the audited CLI surface; nested subpackages are
    private implementation detail and carry no verb-naming obligation.
    """
    # glob on a missing directory yields nothing, so a stale root would let a
    # convention check pass vacuously; fail loudly instead.
    assert directory.is_dir(), f"Script scan root is missing: {directory}"
    return sorted(path for path in directory.glob("*.py") if path.name != "__init__.py")


def _verb(path: Path) -> str:
    """Return the leading action verb from a snake-case script name."""
    return path.stem.partition("_")[0]


def test_user_scripts_use_supported_action_verbs() -> None:
    unexpected = [path.name for path in _python_scripts(SCRIPTS_DIR) if _verb(path) not in USER_SCRIPT_VERBS]
    assert unexpected == [], f"User script names must be action-oriented: {unexpected}"


def test_manual_scripts_do_not_look_like_pytest_files() -> None:
    pytest_like = [
        path.relative_to(SOFTWARE_ROOT).as_posix()
        for path in _python_scripts(SCRIPTS_DIR)
        if path.name.startswith("test_") or path.stem.endswith("_test")
    ]
    assert pytest_like == [], f"Manual scripts must not use pytest-reserved names: {pytest_like}"
