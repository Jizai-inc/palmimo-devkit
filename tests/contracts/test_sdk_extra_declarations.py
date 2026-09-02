"""Dependency contract: a project that imports an SDK feature declares its extra.

`palmimo_sdk` keeps `import palmimo_sdk` dependency-free by importing every
optional third-party package lazily, at the point of use. That design is why a
missing extra survives a clean install, a full test run, and CI: nothing fails
until the feature is actually driven, and the traceback that finally appears
comes from inside the SDK (`ModuleNotFoundError: dynamixel_sdk`, raised in
`Palmimo.connect()`), so it reads as a hardware fault rather than a
dependency one.

Import timing therefore cannot be the check. What a project imports can be:
`from palmimo_sdk import DynamixelDriver` is a statement of intent to drive
that servo bus, whatever the runtime later decides to construct. So the
contract asserts, statically, that every project depending on `palmimo-sdk`
declares the extras behind the SDK names it imports.

The mapping from SDK name to extra is spelled out below rather than derived,
because it cannot be derived: lazy imports mean the import graph says every
feature needs every extra, while module-level imports alone say none of them
need any. `test_extras_map_covers_every_public_sdk_module` and
`test_extras_map_covers_every_public_sdk_symbol` keep the mapping exhaustive,
so a new public module or symbol fails here until it is classified -- the
decision is made once, when the feature is added, instead of being
rediscovered by whoever installs it next.
"""

import ast
import os
import re
import tomllib
from pathlib import Path


SOFTWARE_ROOT = Path(__file__).resolve().parents[2]
SDK_PROJECT = SOFTWARE_ROOT / "packages" / "palmimo_sdk"
SDK_PACKAGE = SDK_PROJECT / "palmimo_sdk"
SDK_DISTRIBUTION = "palmimo-sdk"
SDK_MODULE = "palmimo_sdk"

# palmimo_sdk.io is the SDK's internal I/O layer, banned outside the SDK by
# ruff's TID251 rule (see [tool.ruff.lint.flake8-tidy-imports.banned-api] in
# the root pyproject.toml). It is left unclassified deliberately: an import of
# it fails this contract too, pointing back at the public surface.
INTERNAL_SUBPACKAGE = f"{SDK_MODULE}.io"

_NO_EXTRA: frozenset[str] = frozenset()

#: SDK module -> the extras every name in it needs. A module listed with
#: `_NO_EXTRA` is reachable from the base install alone.
MODULE_EXTRAS: dict[str, frozenset[str]] = {
    # The top-level package re-exports features from all over the SDK, so its
    # answer is per-symbol; SYMBOL_EXTRAS below carries it. This entry is what
    # a bare `import palmimo_sdk` costs, which is nothing.
    SDK_MODULE: _NO_EXTRA,
    # pydantic-validated Tool / ToolResult / AgentToolSet. The only part of the
    # SDK that imports pydantic at module level.
    "palmimo_sdk.agent": frozenset({"agent"}),
    "palmimo_sdk.agent.receiver": frozenset({"agent"}),
    "palmimo_sdk.agent.tools": frozenset({"agent"}),
    "palmimo_sdk.agent.toolset": frozenset({"agent"}),
    # The audio stack (echo cancellation, denoise, WAV conversion) runs on
    # numpy plus the [voice] extra's ONNX runtimes throughout.
    "palmimo_sdk.audio": frozenset({"voice"}),
    "palmimo_sdk.audio.aec": frozenset({"voice"}),
    "palmimo_sdk.audio.denoise": frozenset({"voice"}),
    "palmimo_sdk.audio.dtln": frozenset({"voice"}),
    "palmimo_sdk.audio.processor": frozenset({"voice"}),
    # urllib + hashlib model fetching; no third-party package involved.
    "palmimo_sdk.download": _NO_EXTRA,
    "palmimo_sdk.engine": _NO_EXTRA,
    "palmimo_sdk.kinematics": _NO_EXTRA,
    "palmimo_sdk.mcp": frozenset({"mcp"}),
    "palmimo_sdk.mcp.server": frozenset({"mcp"}),
    # difflib / unicodedata name folding; no third-party package involved.
    "palmimo_sdk.name_match": _NO_EXTRA,
    "palmimo_sdk.robot": _NO_EXTRA,
}

#: `module:Name` -> the extras that name needs, overriding its module's entry.
#: Every name in the top-level package's `__all__` is listed, including the
#: ones that need nothing, so that adding a public name forces the decision.
SYMBOL_EXTRAS: dict[str, frozenset[str]] = {
    # -- [hardware]: the Dynamixel bus. Only the driver itself reaches
    # dynamixel_sdk (io/_dynamixel_bus.py, imported inside connect()); port
    # detection next to it runs on pyserial, which is a base dependency.
    "palmimo_sdk:DynamixelConnectTimeoutError": frozenset({"hardware"}),
    "palmimo_sdk:DynamixelDriver": frozenset({"hardware"}),
    "palmimo_sdk:SUPPORTED_MOTOR_MODELS": _NO_EXTRA,
    "palmimo_sdk:PortDetectionError": _NO_EXTRA,
    "palmimo_sdk:find_servo_port": _NO_EXTRA,
    "palmimo_sdk:palmimo_motor_ids": _NO_EXTRA,
    # -- [face]: the face-display client. Its transport is pyserial, already a
    # base dependency, so the extra installs nothing today -- it is required
    # here so the declaration names the feature rather than leaning on a base
    # dependency that happens to cover it.
    "palmimo_sdk:FaceDisplay": frozenset({"face"}),
    "palmimo_sdk:FaceDisplayConnectTimeoutError": frozenset({"face"}),
    "palmimo_sdk:FaceDisplayError": frozenset({"face"}),
    "palmimo_sdk:find_face_port": frozenset({"face"}),
    # -- [vision]: the head camera, over cv2.
    "palmimo_sdk:HeadCamera": frozenset({"vision"}),
    "palmimo_sdk:HeadCameraConfig": frozenset({"vision"}),
    # -- [voice]: mic capture (sounddevice) and the audio processor chain.
    "palmimo_sdk:AudioProcessor": frozenset({"voice"}),
    "palmimo_sdk:ClipDenoiser": frozenset({"voice"}),
    "palmimo_sdk:Denoiser": frozenset({"voice"}),
    "palmimo_sdk:EchoCanceller": frozenset({"voice"}),
    "palmimo_sdk:MicStream": frozenset({"voice"}),
    "palmimo_sdk:Microphone": frozenset({"voice"}),
    "palmimo_sdk:MicrophoneConfig": frozenset({"voice"}),
    "palmimo_sdk:Subscription": frozenset({"voice"}),
    # -- [speech]: Speaker and its default piper-plus engine. TtsEngine /
    # TtsVoice are the plug-in interface (stdlib only), and OpenAiEngine talks
    # to the API over urllib -- see its _scaled() for why its numpy use is
    # deliberately not an extra.
    "palmimo_sdk:PiperEngine": frozenset({"speech"}),
    "palmimo_sdk:Speaker": frozenset({"speech"}),
    "palmimo_sdk:SpeakerConfig": frozenset({"speech"}),
    "palmimo_sdk:SpeechHandle": frozenset({"speech"}),
    "palmimo_sdk:OpenAiEngine": _NO_EXTRA,
    "palmimo_sdk:TtsEngine": _NO_EXTRA,
    "palmimo_sdk:TtsVoice": _NO_EXTRA,
    # -- The facade, the gait/IK core, and the I/O abstraction: pure
    # computation and stdlib, which is what makes compute-only use possible
    # with no extra at all.
    "palmimo_sdk:Motion": _NO_EXTRA,
    "palmimo_sdk:MotionCancelled": _NO_EXTRA,
    "palmimo_sdk:MotionEngine": _NO_EXTRA,
    "palmimo_sdk:NeckPitchDegrees": _NO_EXTRA,
    "palmimo_sdk:NeckPitchNormalized": _NO_EXTRA,
    "palmimo_sdk:NeckYawDegrees": _NO_EXTRA,
    "palmimo_sdk:NeckYawNormalized": _NO_EXTRA,
    "palmimo_sdk:Palmimo": _NO_EXTRA,
    "palmimo_sdk:RoutineStep": _NO_EXTRA,
    "palmimo_sdk:ServoDriver": _NO_EXTRA,
    "palmimo_sdk:ServoTelemetry": _NO_EXTRA,
    "palmimo_sdk:kinematics": _NO_EXTRA,
    # -- Name matching and ALSA card resolution: stdlib only (difflib,
    # unicodedata, and a subprocess call to aplay/arecord). Both sit beside
    # a feature that does have an extra -- the wake word is heard through
    # [voice], the resolved card is opened by [voice] or [speech] -- but
    # neither reaches a package of its own, so neither requires one. Same
    # reading as OpenAiEngine above.
    "palmimo_sdk:NameMatch": _NO_EXTRA,
    "palmimo_sdk:NameMatcher": _NO_EXTRA,
    "palmimo_sdk:PALMIMO_NAMES": _NO_EXTRA,
    "palmimo_sdk:name_skeleton": _NO_EXTRA,
    "palmimo_sdk:resolve_alsa_device": _NO_EXTRA,
}


def _is_scannable(relative: Path) -> bool:
    """Whether *relative* is tree source rather than a virtualenv or cache copy."""
    return not any(part.startswith(".") or part == "__pycache__" for part in relative.parts)


def _walk_tree() -> tuple[list[Path], list[Path]]:
    """Every `.py` file in the tree, and every directory that is its own project.

    Hidden directories and caches are pruned during the walk rather than
    filtered afterwards: a synced `.venv/` holds tens of thousands of files,
    including installed copies of this tree's own packages and their
    pyproject.toml files, which would be both slow and wrong to read.
    """
    sources: list[Path] = []
    projects: list[Path] = []
    for directory, subdirectories, filenames in os.walk(SOFTWARE_ROOT):
        subdirectories[:] = [name for name in subdirectories if not name.startswith(".") and name != "__pycache__"]
        here = Path(directory)
        sources.extend(here / name for name in filenames if name.endswith(".py"))
        if "pyproject.toml" in filenames:
            projects.append(here)
    return sorted(sources), sorted(projects)


def _owning_project(source: Path, roots: list[Path]) -> Path:
    """The deepest project root containing *source*.

    Nearest ancestor, not first match: `examples/agents/wakeword/` sits inside
    the workspace root, and the LeRobot members inside their own workspace, so
    only the innermost pyproject.toml describes what a file may import.
    """
    return max((root for root in roots if root in source.parents), key=lambda root: len(root.parts))


def _sdk_imports(source: Path) -> dict[tuple[str, str | None], Path]:
    """SDK names *source* imports, mapped to *source* so failures can cite a file.

    A key is `(module, name)` for `from <module> import <name>`, or
    `(module, None)` for `import <module>`. Attribute access through a bound
    `import palmimo_sdk` is resolved too, so the check does not depend on
    which import form a caller chose.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    nodes = list(ast.walk(tree))
    imports: dict[tuple[str, str | None], Path] = {}
    # Local name -> the SDK module it is bound to, for the attribute pass below.
    bound: dict[str, str] = {}

    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name != SDK_MODULE and not alias.name.startswith(f"{SDK_MODULE}."):
                    continue
                imports[(alias.name, None)] = source
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    # `import palmimo_sdk.mcp` binds the root package name.
                    bound[SDK_MODULE] = SDK_MODULE
        elif isinstance(node, ast.ImportFrom):
            # node.level > 0 is a relative import, which never reaches the SDK.
            if node.level or not node.module:
                continue
            if node.module == SDK_MODULE or node.module.startswith(f"{SDK_MODULE}."):
                for alias in node.names:
                    imports[(node.module, alias.name)] = source

    # A second pass, so the bindings above are complete whatever order ast.walk
    # reached them in. Dunders (`palmimo_sdk.__version__`) are module metadata,
    # not features, and carry no extra.
    for node in nodes:
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and not node.attr.startswith("_"):
            module = bound.get(node.value.id)
            if module is not None:
                imports[(module, node.attr)] = source

    return imports


def _extras_for(module: str, name: str | None) -> frozenset[str] | None:
    """Extras an import of *name* from *module* needs, or None if unclassified.

    A name that is itself a module wins (`from palmimo_sdk import kinematics`),
    then a symbol entry, then the module's own entry. The top-level package is
    the exception: its entry describes a bare `import palmimo_sdk` only, so a
    name taken from it must be classified individually or stay unclassified.
    """
    if name is None:
        return MODULE_EXTRAS.get(module)
    submodule = MODULE_EXTRAS.get(f"{module}.{name}")
    if submodule is not None:
        return submodule
    symbol = SYMBOL_EXTRAS.get(f"{module}:{name}")
    if symbol is not None or module == SDK_MODULE:
        return symbol
    return MODULE_EXTRAS.get(module)


def _declared_sdk_extras(project: Path) -> frozenset[str] | None:
    """Extras *project* declares on palmimo-sdk, or None if it does not depend on it."""
    manifest = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    for requirement in manifest.get("project", {}).get("dependencies", []):
        # Strip any environment marker, then read the name and extras off the front.
        match = re.match(r"\s*(?P<name>[A-Za-z0-9._-]+)\s*(?:\[(?P<extras>[^\]]*)\])?", requirement.split(";", 1)[0])
        if match is None or _canonical(match["name"]) != SDK_DISTRIBUTION:
            continue
        return frozenset(_canonical(extra) for extra in (match["extras"] or "").split(",") if extra.strip())
    return None


def _canonical(name: str) -> str:
    """PEP 503 normalized form, so `palmimo_sdk` and `palmimo-sdk` compare equal."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _public_sdk_modules() -> set[str]:
    """Dotted names of the SDK's public modules.

    Skips private modules, `__main__` entry points, the package's own tests,
    and the internal `palmimo_sdk.io` layer (see INTERNAL_SUBPACKAGE).
    """
    modules: set[str] = set()
    for path in SDK_PACKAGE.rglob("*.py"):
        relative = path.relative_to(SDK_PACKAGE.parent)
        if not _is_scannable(relative) or "tests" in relative.parts:
            continue
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if any(part.startswith("_") for part in parts):
            continue
        dotted = ".".join(parts)
        if dotted == INTERNAL_SUBPACKAGE or dotted.startswith(f"{INTERNAL_SUBPACKAGE}."):
            continue
        modules.add(dotted)
    return modules


def _public_sdk_names() -> list[str]:
    """The top-level package's `__all__`, read without importing it."""
    tree = ast.parse((SDK_PACKAGE / "__init__.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(target, "id", "") == "__all__" for target in node.targets):
            assert isinstance(node.value, ast.List)
            return [
                element.value
                for element in node.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
    raise AssertionError(f"{SDK_PACKAGE / '__init__.py'} defines no __all__")


def _declared_sdk_extra_names() -> set[str]:
    """The extras palmimo-sdk actually offers."""
    manifest = tomllib.loads((SDK_PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(manifest["project"]["optional-dependencies"])


def test_extras_map_covers_every_public_sdk_module() -> None:
    """Every public SDK module is classified, and nothing stale is left behind."""
    assert _public_sdk_modules() == set(MODULE_EXTRAS), (
        "MODULE_EXTRAS must list exactly the SDK's public modules. A new module needs an entry saying "
        "which extras its contents require (frozenset() when it needs none); a removed one needs its "
        "entry dropped."
    )


def test_extras_map_covers_every_public_sdk_symbol() -> None:
    """Every name palmimo_sdk exports is classified, including the free ones."""
    classified = {key.split(":", 1)[1] for key in SYMBOL_EXTRAS if key.startswith(f"{SDK_MODULE}:")}
    assert set(_public_sdk_names()) == classified, (
        "SYMBOL_EXTRAS must classify exactly palmimo_sdk.__all__. Adding a public name is where its extra "
        "gets decided -- otherwise the decision falls to whoever installs the SDK and finds it missing."
    )


def test_extras_map_names_only_extras_the_sdk_offers() -> None:
    """No entry points at an extra palmimo-sdk does not define."""
    mapped = set().union(*MODULE_EXTRAS.values(), *SYMBOL_EXTRAS.values())
    offered = _declared_sdk_extra_names()
    assert mapped <= offered, (
        f"MODULE_EXTRAS/SYMBOL_EXTRAS name extras palmimo-sdk does not define: {sorted(mapped - offered)}. "
        "Renaming an extra means updating both sides."
    )


def test_every_project_declares_the_sdk_extras_it_imports() -> None:
    """A project that imports an SDK feature declares the extra carrying it.

    Covers every project in the tree except palmimo-sdk itself, which defines
    the extras rather than consuming them. Each `.py` file is attributed to
    the nearest enclosing project, so an example's imports are judged against
    the example's own manifest, not the workspace root's.
    """
    sources, roots = _walk_tree()
    imports_by_project: dict[Path, dict[tuple[str, str | None], Path]] = {root: {} for root in roots}
    for source in sources:
        imports_by_project[_owning_project(source, roots)].update(_sdk_imports(source))

    undeclared: list[str] = []
    unclassified: list[str] = []
    for root in roots:
        if root == SDK_PROJECT:
            continue
        label = root.relative_to(SOFTWARE_ROOT).as_posix()
        imports = imports_by_project[root]
        declared = _declared_sdk_extras(root)

        required: dict[str, list[str]] = {}
        for (module, name), source in sorted(imports.items(), key=lambda item: (item[0][0], item[0][1] or "")):
            extras = _extras_for(module, name)
            witness = f"{module}.{name}" if name else module
            if extras is None:
                unclassified.append(f"{label}: {witness} ({source.relative_to(SOFTWARE_ROOT).as_posix()})")
                continue
            for extra in extras:
                required.setdefault(extra, []).append(witness)

        if declared is None:
            if imports:
                undeclared.append(f"{label}: imports {SDK_MODULE} without depending on {SDK_DISTRIBUTION}")
            continue
        missing = sorted(set(required) - declared)
        if missing:
            reasons = "; ".join(f"{extra} <- {', '.join(sorted(set(required[extra]))[:3])}" for extra in missing)
            undeclared.append(
                f"{label}: declares {SDK_DISTRIBUTION}[{','.join(sorted(declared))}], "
                f"missing {','.join(missing)} ({reasons})"
            )

    assert not unclassified, (
        "Imports of SDK names this contract cannot classify:\n  "
        + "\n  ".join(unclassified)
        + f"\nA name under {INTERNAL_SUBPACKAGE} is the SDK's internal layer -- import the equivalent from "
        f"{SDK_MODULE} instead. Anything else needs a MODULE_EXTRAS/SYMBOL_EXTRAS entry."
    )
    assert not undeclared, (
        "Projects importing SDK features whose extras they do not declare:\n  "
        + "\n  ".join(undeclared)
        + "\nAdd the extras to the palmimo-sdk requirement in that project's pyproject.toml. Nothing fails at "
        "import time -- the feature dies when it is first driven, which for the servo bus means on hardware."
    )
