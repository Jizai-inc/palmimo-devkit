"""SPDX header contract for the published SDK core.

The OSS-release licensing policy settled the scope: an `SPDX-License-Identifier` header is
required only on `palmimo_sdk` package code — the tree that a downstream
consumer imports and that a license scanner attributes to this project.
Examples, tests, and scripts are deliberately out of scope; they are demo /
verification code, not distributable package sources, so this contract does
not touch them.
"""

from pathlib import Path


SOFTWARE_ROOT = Path(__file__).resolve().parents[2]
PALMIMO_SDK_ROOT = SOFTWARE_ROOT / "packages" / "palmimo_sdk" / "palmimo_sdk"

SPDX_HEADER = "# SPDX-License-Identifier: Apache-2.0"


def _package_sources() -> list[Path]:
    """Return every `palmimo_sdk` package source file, excluding its own tests.

    `tests/` under the package is verification code, not a distributed module,
    so it carries no SPDX obligation even though it lives inside the package
    directory.
    """
    assert PALMIMO_SDK_ROOT.is_dir(), f"palmimo_sdk package root is missing: {PALMIMO_SDK_ROOT}"
    return sorted(
        path for path in PALMIMO_SDK_ROOT.rglob("*.py") if "tests" not in path.relative_to(PALMIMO_SDK_ROOT).parts
    )


def test_palmimo_sdk_package_sources_carry_an_spdx_header() -> None:
    sources = _package_sources()
    assert sources, f"No package sources were found under {PALMIMO_SDK_ROOT}; the contract would pass vacuously."
    missing = [
        path.relative_to(SOFTWARE_ROOT).as_posix()
        for path in sources
        if SPDX_HEADER not in path.read_text(encoding="utf-8")
    ]
    assert missing == [], (
        f"These palmimo_sdk package sources are missing '{SPDX_HEADER}': {missing}. "
        "Add it as the first line, per the OSS-release licensing policy's core-only SPDX rule."
    )
