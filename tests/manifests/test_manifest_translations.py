"""All official descriptions have validated, complete Japanese translations."""

import json
import tomllib
from pathlib import Path

import pytest

from tools.build_catalog import build_catalog, discover_manifests
from tools.manifest import load_manifest
from tools.manifest_translations import load_translations


def test_official_japanese_translations_cover_every_description() -> None:
    catalog = {entry["name"]: entry for entry in build_catalog("examples-v1", "a" * 40)["apps"]}
    for path in discover_manifests():
        raw = tomllib.loads(path.read_text())
        manifest = load_manifest(path)
        expected = (
            {"description"}
            | {f"env.{key}" for key in raw.get("env", {})}
            | {f"params.{key}" for key, spec in raw.get("params", {}).items() if "description" in spec}
        )
        assert set(manifest.translations["ja"]) == expected
        assert catalog[manifest.name]["translations"] == manifest.translations


@pytest.mark.parametrize(
    "message", [{"params.typo": "Text"}, {"description": ""}, {"description": "x" * 201}, {"command": "run"}]
)
def test_translation_validation_rejects_unknown_or_invalid_text(tmp_path: Path, message: dict[str, str]) -> None:
    path = tmp_path / "palmimo.toml"
    path.with_name("palmimo.toml.i18n.json").write_text(json.dumps({"schema": 1, "translations": {"ja": message}}))
    with pytest.raises(ValueError):
        load_translations(path, set(), set())


def test_translations_are_optional_for_existing_manifests(tmp_path: Path) -> None:
    assert load_translations(tmp_path / "palmimo.toml", set(), set()) == {}


@pytest.mark.parametrize(
    ("key", "accepted"),
    [
        ("tags.dance", True),
        ("tags. ＤＡＮＣＥ ", True),
        ("tags. music　 box ", True),
        ("tags.Ärger", True),
        ("tags.ärger", False),
        ("tags.unknown", False),
        ("tags.dan\tce", False),
        ("tags.", False),
        ("tags.dance.extra", False),
    ],
)
def test_tag_translations_match_normalized_declared_tags(tmp_path: Path, key: str, accepted: bool) -> None:
    path = tmp_path / "palmimo.toml"
    path.write_text(
        'schema = 1\nname = "listing"\ndescription = "Listing"\ncommand = ["run"]\n'
        'tags = ["Dance", "music box", "Ärger"]\n'
    )
    path.with_name("palmimo.toml.i18n.json").write_text(
        json.dumps({"schema": 1, "translations": {"ja": {key: "Translated"}}})
    )
    if accepted:
        normalized = {"tags. ＤＡＮＣＥ ": "tags.dance", "tags. music　 box ": "tags.music box"}.get(key, key)
        assert load_manifest(path).translations == {"ja": {normalized: "Translated"}}
    else:
        with pytest.raises(ValueError):
            load_manifest(path)
