"""Select example CI overlays compatible with the workspace SDK version."""

import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


def allows_sdk_overlay(dependencies: Sequence[str], sdk_version: str) -> bool:
    """Return whether the project's SDK requirement accepts the local version."""
    for dependency in dependencies:
        requirement = Requirement(dependency)
        if canonicalize_name(requirement.name) == "palmimo-sdk":
            return Version(sdk_version) in requirement.specifier
    return False


def main() -> None:
    """Emit the overlay decision as a GitHub Actions step output."""
    example_path, sdk_path = map(Path, sys.argv[1:])
    example = tomllib.loads(example_path.read_text())["project"]
    sdk = tomllib.loads(sdk_path.read_text())["project"]
    enabled = allows_sdk_overlay(example.get("dependencies", []), sdk["version"])
    print(f"enabled={str(enabled).lower()}")


if __name__ == "__main__":
    main()
