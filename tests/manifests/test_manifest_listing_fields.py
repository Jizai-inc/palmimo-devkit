"""Listing metadata follows the shared cross-validator cases."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from tools.manifest import ManifestError, load_manifest


CASES = json.loads((Path(__file__).parents[1] / "fixtures/manifests/listing_fields.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_manifest_listing_fields_follow_shared_cases(tmp_path: Path, case: dict) -> None:
    path = tmp_path / "palmimo.toml"
    fields = {"schema": 1, "name": "listing", "description": "Listing app", "command": ["run"], **case["input"]}
    path.write_text("\n".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items()))
    expected = case["expected"]
    if not expected["ok"]:
        with pytest.raises(ManifestError) as caught:
            load_manifest(path)
        assert {error.split("'", 2)[1] for error in caught.value.errors} == {expected["path"]}
        return
    manifest = load_manifest(path)
    actual = {
        "ok": True,
        "tags": list(manifest.tags),
        "tag_keys": list(manifest.tag_keys),
        "thumbnail": {key: value for key, value in asdict(manifest.thumbnail).items() if value is not None}
        if manifest.thumbnail is not None
        else None,
    }
    assert {key: actual[key] for key in expected} == expected
