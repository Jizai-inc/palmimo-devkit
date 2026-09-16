"""Behavior of the release catalog generator (tools/build_catalog.py)."""

from pathlib import Path

import pytest

from tools.build_catalog import build_catalog, discover_manifests
from tools.manifest import load_manifest


def test_catalog_build_is_deterministic() -> None:
    first = build_catalog("v1.2.3")
    second = build_catalog("v1.2.3")
    assert first == second


def test_catalog_apps_are_sorted_by_name() -> None:
    catalog = build_catalog("v1.2.3")
    names = [app["name"] for app in catalog["apps"]]
    assert names == sorted(names)


def test_catalog_entry_env_and_devices_match_the_source_manifest() -> None:
    catalog = build_catalog("v1.2.3")
    entries_by_name = {app["name"]: app for app in catalog["apps"]}

    for manifest_path in discover_manifests():
        manifest = load_manifest(manifest_path)
        entry = entries_by_name[manifest.name]
        assert entry["devices"] == sorted(manifest.devices)
        assert entry["env"] == {
            env_name: {"required": env.required, "description": env.description}
            for env_name, env in manifest.env.items()
        }


def test_catalog_entry_source_points_at_the_requested_tag() -> None:
    catalog = build_catalog("v9.9.9")
    for entry in catalog["apps"]:
        assert entry["source"]["ref"] == "v9.9.9"
        assert entry["source"]["ref_kind"] == "tag"
        assert entry["source"]["type"] == "git"


def test_catalog_covers_every_example_manifest() -> None:
    catalog = build_catalog("v1.2.3")
    catalog_names = {app["name"] for app in catalog["apps"]}
    manifest_names = {load_manifest(path).name for path in discover_manifests()}
    assert catalog_names == manifest_names
    assert catalog_names  # would pass vacuously if discovery ever found nothing


def test_catalog_entry_manifest_key_present_only_for_non_default_filename() -> None:
    catalog = build_catalog("v1.2.3")
    entries_by_name = {app["name"]: app for app in catalog["apps"]}

    realtime = entries_by_name["palmimo-companion-realtime"]
    assert realtime["source"]["manifest"] == "palmimo.realtime.toml"

    pipeline = entries_by_name["palmimo-companion-agent"]
    assert "manifest" not in pipeline["source"]


def test_build_catalog_rejects_duplicate_app_names(tmp_path: Path) -> None:
    app_dir = tmp_path / "examples" / "dup"
    app_dir.mkdir(parents=True)
    manifest_body = (
        'schema = 1\nname = "palmimo-dup"\ndescription = "d"\ncommand = ["true"]\n'
    )
    (app_dir / "palmimo.toml").write_text(manifest_body)
    (app_dir / "palmimo.other.toml").write_text(manifest_body)

    with pytest.raises(ValueError, match="palmimo-dup"):
        build_catalog("v1.2.3", root=tmp_path)


def test_write_catalog_writes_deterministic_bytes(tmp_path: Path) -> None:
    from tools.build_catalog import write_catalog

    out_a = tmp_path / "a.json"
    out_b = tmp_path / "b.json"
    write_catalog("v1.0.0", out_a)
    write_catalog("v1.0.0", out_b)
    assert out_a.read_bytes() == out_b.read_bytes()
