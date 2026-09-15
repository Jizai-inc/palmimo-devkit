"""Build `palmimo-catalog-<tag>.json`, the official-apps catalog Portal reads from a devkit Release.

Scans `examples/*/palmimo.toml` and `examples/agents/*/palmimo.toml` (the same
example apps `.github/workflows/release.yml` ships in the catalog asset),
validates each against doc/reference/app-manifest.md, and writes one catalog
entry per app: name, description, its env/devices exactly as declared (so the
catalog never drifts from the manifest it was built from), and a `source`
pointing at this repository's git subdir, pinned to *tag*.

Usage:
    uv run python -m tools.build_catalog <tag> <output-path>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.manifest import AppManifest, load_manifest


REPO_ROOT = Path(__file__).resolve().parents[1]
DEVKIT_REPO_URL = "https://github.com/Jizai-inc/palmimo-devkit"

# `examples/*/palmimo.toml` and `examples/agents/*/palmimo.toml` cover every
# app example without also matching the grouping directory
# `examples/agents/` (which carries no manifest of its own) -- the same two
# globs `[tool.uv.workspace]` in pyproject.toml uses for the same directories.
MANIFEST_GLOBS = ("examples/*/palmimo.toml", "examples/agents/*/palmimo.toml")


def discover_manifests() -> list[Path]:
    """Return every `palmimo.toml` under `examples/`, sorted for deterministic output."""
    paths = {path for pattern in MANIFEST_GLOBS for path in REPO_ROOT.glob(pattern)}
    return sorted(paths)


def _catalog_entry(manifest_path: Path, manifest: AppManifest, tag: str) -> dict:
    subdir = manifest_path.parent.relative_to(REPO_ROOT).as_posix()
    return {
        "name": manifest.name,
        "description": manifest.description,
        "env": {
            env_name: {"required": env.required, "description": env.description}
            for env_name, env in sorted(manifest.env.items())
        },
        "devices": sorted(manifest.devices),
        "source": {
            "type": "git",
            "url": DEVKIT_REPO_URL,
            "subdir": subdir,
            "ref_kind": "tag",
            "ref": tag,
        },
    }


def build_catalog(tag: str) -> dict:
    """Build the catalog document for *tag* from every example manifest."""
    entries = []
    for manifest_path in discover_manifests():
        manifest = load_manifest(manifest_path)
        entries.append(_catalog_entry(manifest_path, manifest, tag))
    entries.sort(key=lambda entry: entry["name"])
    return {"schema": 1, "apps": entries}


def write_catalog(tag: str, output_path: Path) -> None:
    catalog = build_catalog(tag)
    # Trailing newline, sorted keys off (insertion order is already
    # deterministic -- see _catalog_entry) so a re-run over the same tag
    # produces byte-identical output.
    output_path.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="Release tag the generated 'source.ref' points at, e.g. v0.3.0")
    parser.add_argument("output", type=Path, help="Path to write the catalog JSON to")
    args = parser.parse_args()
    write_catalog(args.tag, args.output)


if __name__ == "__main__":
    main()
