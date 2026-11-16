"""Schema 2 asset declarations and filesystem boundaries at catalog publication."""

import json
from pathlib import Path

import pytest

from tools.build_catalog import build_catalog
from tools.manifest import ManifestError, load_manifest


BASE = 'schema = {schema}\nname = "asset-demo"\ndescription = "Demo"\ncommand = ["run"]\n'
ASSET = '[assets.welcome]\nfile = "assets/face.json"\ndescription = "Welcome a visitor"\n'
PARAM = '[assets.welcome.params.eye_color]\ndescription = "Eye color"\n'


@pytest.fixture
def app(tmp_path: Path) -> Path:
    directory = tmp_path / "examples" / "demo"
    (directory / "assets").mkdir(parents=True)
    animation = {
        "v": "5.7.4",
        "fr": 30,
        "ip": 0,
        "op": 30,
        "w": 480,
        "h": 480,
        "slots": {"eye_color": {"p": {"a": 0, "k": [1, 0, 0, 1]}}},
        "layers": [{"ty": 4, "shapes": [{"ty": "fl", "c": {"sid": "eye_color", "a": 0, "k": [1, 0, 0, 1]}}]}],
    }
    (directory / "assets" / "face.json").write_text(json.dumps(animation))
    for suffix in ("gif", "png", "jpg", "jpeg"):
        (directory / "assets" / f"face.{suffix}").write_bytes(b"image converted at install time")
    return directory


@pytest.mark.parametrize("layout", ["none", "inline", "include", "gif", "png", "jpg", "jpeg"])
def test_catalog_build_accepts_schema_two_assets(app: Path, layout: str) -> None:
    body = BASE.format(schema=2)
    if layout == "inline":
        body += ASSET + 'tags = ["greeting"]\n' + PARAM
    elif layout == "include":
        (app / "assets" / "more.toml").write_text(ASSET + PARAM)
        body += (
            '[assets]\ninclude = "assets/more.toml"\n[assets.extra]\nfile = "assets/face.png"\ndescription = "Extra"\n'
        )
    elif layout != "none":
        body += ASSET.replace("face.json", f"face.{layout}")
    path = app / "palmimo.toml"
    path.write_text(body)

    manifest = load_manifest(path)
    assert manifest.schema == 2
    if layout in {"inline", "include"}:
        assert manifest.assets["welcome"].params == {"eye_color": "Eye color"}
        assert set(manifest.assets) == ({"welcome", "extra"} if layout == "include" else {"welcome"})
    catalog = build_catalog("examples-v0.2.0", "a" * 40, root=app.parents[1])
    assert [entry["name"] for entry in catalog["apps"]] == ["asset-demo"]
    assert catalog["apps"][0]["source"]["subdir"] == "examples/demo"


def test_schema_one_rejects_assets(app: Path) -> None:
    path = app / "palmimo.toml"
    path.write_text(BASE.format(schema=1) + ASSET)
    with pytest.raises(ManifestError):
        load_manifest(path)


@pytest.mark.parametrize(
    "body",
    [
        'assets = "invalid"\n',
        ASSET.replace("welcome", "Uppercase"),
        ASSET.replace("welcome", "a" * 41),
        ASSET.replace('file = "assets/face.json"\n', ""),
        ASSET.replace('"assets/face.json"', "42"),
        ASSET.replace('"assets/face.json"', '""'),
        ASSET.replace('"assets/face.json"', '"../face.json"'),
        ASSET.replace('"assets/face.json"', '"/face.json"'),
        ASSET.replace('"assets/face.json"', '"assets/missing.json"'),
        ASSET.replace('"assets/face.json"', '"assets"'),
        ASSET.replace('"assets/face.json"', '"assets/face.bmp"'),
        ASSET.replace('description = "Welcome a visitor"\n', ""),
        ASSET.replace('"Welcome a visitor"', '""'),
        ASSET.replace('"Welcome a visitor"', "42"),
        ASSET.replace('"Welcome a visitor"', '"' + "a" * 201 + '"'),
        ASSET + "unknown = true\n",
        ASSET + 'tags = "greeting"\n',
        ASSET + 'tags = ["Uppercase"]\n',
        ASSET + "tags = [42]\n",
        ASSET + 'tags = ["' + "a" * 33 + '"]\n',
        ASSET + 'params = "invalid"\n',
        ASSET.replace("face.json", "face.png") + "[assets.welcome.params]\n",
        ASSET + PARAM.replace("eye_color", "unknown"),
        ASSET + PARAM.replace("eye_color", "Bad-Slot"),
        ASSET + PARAM.replace('description = "Eye color"', 'value = "Eye color"'),
        ASSET + PARAM.replace('"Eye color"', '""'),
        ASSET + PARAM.replace('"Eye color"', "42"),
        ASSET + PARAM.replace('"Eye color"', '"' + "a" * 201 + '"'),
        ASSET + PARAM + "extra = true\n",
        ASSET + 'params = {eye_color = "invalid"}\n',
    ],
)
def test_invalid_asset_declaration_is_rejected(app: Path, body: str) -> None:
    (app / "assets" / "face.bmp").write_bytes(b"unsupported")
    path = app / "palmimo.toml"
    path.write_text(BASE.format(schema=2) + body)
    with pytest.raises(ManifestError):
        load_manifest(path)


@pytest.mark.parametrize("contents", ["not json", "[]", '{"slots": []}', '{"slots": {}}'])
def test_asset_parameters_require_a_slot_in_the_lottie_file(app: Path, contents: str) -> None:
    (app / "assets" / "face.json").write_text(contents)
    path = app / "palmimo.toml"
    path.write_text(BASE.format(schema=2) + ASSET + PARAM)
    with pytest.raises(ManifestError):
        load_manifest(path)


@pytest.mark.parametrize(
    "include,contents",
    [
        ("missing.toml", None),
        ("../more.toml", ASSET),
        ("/more.toml", ASSET),
        ("more.toml", "invalid toml"),
        ("more.toml", "other = 1\n" + ASSET),
        ("more.toml", "assets = []"),
        ("more.toml", '[assets]\ninclude = "other.toml"'),
        ("more.toml", ASSET),
    ],
)
def test_invalid_asset_include_is_rejected(app: Path, include: str, contents: str | None) -> None:
    if contents is not None:
        (app / "more.toml").write_text(contents)
    path = app / "palmimo.toml"
    path.write_text(BASE.format(schema=2) + "[assets]\ninclude = " + json.dumps(include) + "\n" + ASSET)
    with pytest.raises(ManifestError):
        load_manifest(path)


@pytest.mark.parametrize("target", ["asset", "directory", "include"])
def test_asset_paths_reject_symlinks(app: Path, target: str) -> None:
    path = app / "palmimo.toml"
    body = BASE.format(schema=2)
    if target == "asset":
        (app / "face.json").symlink_to(app / "assets" / "face.json")
        body += ASSET.replace("assets/face.json", "face.json")
    elif target == "directory":
        (app / "linked").symlink_to(app / "assets", target_is_directory=True)
        body += ASSET.replace("assets/face.json", "linked/face.json")
    else:
        (app / "more.toml").write_text(ASSET)
        (app / "linked.toml").symlink_to(app / "more.toml")
        body += '[assets]\ninclude = "linked.toml"\n'
    path.write_text(body)
    with pytest.raises(ManifestError):
        load_manifest(path)


@pytest.mark.parametrize("schema", ["0", "3", "true", "1.0", '"2"'])
def test_manifest_rejects_unsupported_schema(app: Path, schema: str) -> None:
    path = app / "palmimo.toml"
    path.write_text(BASE.format(schema=schema))
    with pytest.raises(ManifestError):
        load_manifest(path)


@pytest.mark.parametrize(
    "key", ["assets.welcome", "assets.welcome.params.eye_color", "assets.typo", "assets.welcome.params.typo"]
)
def test_asset_translations_follow_validated_declarations(app: Path, key: str) -> None:
    path = app / "palmimo.toml"
    path.write_text(BASE.format(schema=2) + ASSET + PARAM)
    messages = {key: "Localized asset text"}
    path.with_name("palmimo.toml.i18n.json").write_text(json.dumps({"schema": 1, "translations": {"ja": messages}}))
    if "typo" in key:
        with pytest.raises(ValueError):
            load_manifest(path)
    else:
        assert load_manifest(path).translations["ja"] == messages
        catalog = build_catalog("examples-v0.2.0", "a" * 40, root=app.parents[1])
        assert catalog["apps"][0]["translations"]["ja"] == messages
