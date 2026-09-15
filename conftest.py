"""Root pytest configuration.

`tools/` (maintainer scripts, e.g. the app-manifest validator and the release
catalog builder -- see tools/manifest.py) is a plain top-level directory, not
a workspace member with its own `pyproject.toml`, so nothing installs it into
the venv's `site-packages`. `uv run pytest` invokes the `pytest` console
script directly, which does not put the current directory on `sys.path` the
way `python -m pytest` or a plain interpreter session would -- so `import
tools.manifest` from a test needs this repository root on the path explicitly.
"""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
