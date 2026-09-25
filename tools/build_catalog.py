"""Build `palmimo-catalog-<tag>.json`, the official-apps catalog Portal reads from an examples Release.

Scans every manifest file (`palmimo.toml` or `palmimo.<variant>.toml`) in
`examples/*` and `examples/agents/*` (the same example directories
`.github/workflows/release.yml` ships in an examples-tagged release) -- one app per
manifest file, so a directory may ship several -- validates each against
doc/reference/app-manifest.md, and writes one catalog entry per app: name,
description, its env/devices exactly as declared (so the catalog never
drifts from the manifest it was built from), and a `source` pointing at this
repository's git subdir, pinned to *tag*, plus a `manifest` filename when the
app's file isn't the default `palmimo.toml`.

Usage:
    uv run python -m tools.build_catalog <tag> <commit> <output-path>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.manifest import AppManifest, EnvVar, load_manifest
from tools.manifest import discover_manifests as _discover_manifests_in_dir


REPO_ROOT = Path(__file__).resolve().parents[1]
DEVKIT_REPO_URL = "https://github.com/Jizai-inc/palmimo-devkit"

# `examples/*` and `examples/agents/*` cover every app example directory
# without also matching the grouping directory `examples/agents/` itself
# (which carries no manifest of its own) -- the same two globs
# `[tool.uv.workspace]` in pyproject.toml uses for the same directories.
APP_DIR_GLOBS = ("examples/*", "examples/agents/*")


def discover_manifests(root: Path = REPO_ROOT) -> list[Path]:
    """Return every manifest file under *root*'s example app directories, sorted for deterministic output.

    Scoped to the two example-app directory levels (not recursive within
    each), so a manifest-shaped file inside an app's own package (e.g.
    `palmimo_companion_agent/`) is never picked up.
    """
    app_dirs = {path for pattern in APP_DIR_GLOBS for path in root.glob(pattern) if path.is_dir()}
    paths = [manifest for app_dir in app_dirs for manifest in _discover_manifests_in_dir(app_dir)]
    return sorted(paths)


def _env_entry(env: EnvVar) -> dict:
    entry: dict = {"required": env.required, "description": env.description}
    if env.help_url is not None:
        entry["help_url"] = env.help_url
    return entry


def _catalog_entry(manifest_path: Path, manifest: AppManifest, tag: str, commit: str, root: Path) -> dict:
    subdir = manifest_path.parent.relative_to(root).as_posix()
    source = {
        "type": "git",
        "url": DEVKIT_REPO_URL,
        "subdir": subdir,
        "ref_kind": "tag",
        "ref": tag,
        "commit": commit,
    }
    if manifest_path.name != "palmimo.toml":
        source["manifest"] = manifest_path.name
    return {
        "name": manifest.name,
        "description": manifest.description,
        "env": {env_name: _env_entry(env) for env_name, env in sorted(manifest.env.items())},
        "devices": sorted(manifest.devices),
        "source": source,
    }


def build_catalog(tag: str, commit: str, root: Path = REPO_ROOT) -> dict:
    """Build the catalog document for *tag* from every example manifest under *root*.

    Raises:
        ValueError: if two manifests declare the same `name` -- the catalog
            is keyed by name, so a collision would silently drop one app.
    """
    entries = []
    seen_names: dict[str, Path] = {}
    for manifest_path in discover_manifests(root):
        manifest = load_manifest(manifest_path)
        if manifest.name in seen_names:
            raise ValueError(f"duplicate app name {manifest.name!r} in {manifest_path} and {seen_names[manifest.name]}")
        seen_names[manifest.name] = manifest_path
        entries.append(_catalog_entry(manifest_path, manifest, tag, commit, root))
    entries.sort(key=lambda entry: entry["name"])
    return {"schema": 1, "apps": entries}


def write_catalog(tag: str, commit: str, output_path: Path) -> None:
    catalog = build_catalog(tag, commit)
    # Trailing newline, sorted keys off (insertion order is already
    # deterministic -- see _catalog_entry) so a re-run over the same tag
    # produces byte-identical output.
    output_path.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="Examples release tag the generated 'source.ref' points at, e.g. examples-v0.3.0")
    parser.add_argument("commit", help="Commit SHA the examples release tag points at")
    parser.add_argument("output", type=Path, help="Path to write the catalog JSON to")
    args = parser.parse_args()
    write_catalog(args.tag, args.commit, args.output)


if __name__ == "__main__":
    main()
