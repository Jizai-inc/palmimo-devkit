"""Attribution contract: `NOTICE` must index every project's notices, for real.

Apache-2.0 §4(d) makes `NOTICE` part of what ships, and this tree's `NOTICE`
discharges that by pointing at four `THIRD_PARTY_NOTICES.md` files rather than
inlining them. That turns two things into silent-breakage risks: a pointer that
outlives the file it names, and a workspace member added later whose
dependencies never reach the index at all. Both are decidable from the tree, so
they are asserted here instead of relying on review.

The link check covers every relative reference in `NOTICE` and in the notice
files themselves, because the files cross-reference each other (the root file
defers `dynamixel-sdk` to the SDK's, the SDK's defers the second bundled FFmpeg
to the companion example's) and a stale pointer reads as an absent attribution.
Every such reference must also land inside this tree: a path that climbs out of
it resolves nowhere in the published repository.

Indexing is necessary but not sufficient, so one contract goes further and
reads the notices' content: every third-party distribution any project in this
tree declares must actually be attributed somewhere. An index of files that say
nothing satisfies every other test here. That check is what catches the case
this suite was written for -- a dependency whose environment marker keeps it
off the machine the author develops on, and on the machine the product ships
on.
"""

import re
import tomllib
from pathlib import Path


SOFTWARE_ROOT = Path(__file__).resolve().parents[2]
NOTICE = SOFTWARE_ROOT / "NOTICE"
NOTICE_FILENAME = "THIRD_PARTY_NOTICES.md"

# Workspace members that legitimately have no notice file of their own. A member
# earns a place here only by declaring no third-party dependencies at all, which
# is asserted below rather than trusted; "nobody has written it yet" is the
# failure this test exists to catch.
MEMBERS_WITHOUT_NOTICES: frozenset[str] = frozenset()

_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
# A requirement string ends its name at the first version specifier, extra,
# marker, or whitespace: "sherpa-onnx-core>=1.13.0 ; platform_machine == ..."
# and "palmimo-sdk[voice]" both name a single distribution.
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _load(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def _normalize(name: str) -> str:
    """PEP 503 normalization, so `sherpa_onnx_core` and `sherpa-onnx-core` agree."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _notice_referenced_paths() -> list[str]:
    """Relative paths listed in the plain-text `NOTICE` index.

    `NOTICE` is not Markdown -- the entries are bare paths in a bulleted list,
    so they are matched by shape (a leading `- ` and a `.md` suffix) rather
    than by link syntax.
    """
    paths = []
    for line in NOTICE.read_text(encoding="utf-8").splitlines():
        entry = line.strip().removeprefix("-").strip()
        if entry.endswith(".md"):
            paths.append(entry)
    return paths


def _notice_files() -> list[Path]:
    return sorted(
        path
        for path in SOFTWARE_ROOT.rglob(NOTICE_FILENAME)
        if not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(SOFTWARE_ROOT).parts)
    )


def _project_dirs() -> list[Path]:
    """Every directory in this workspace holding a `pyproject.toml`.

    The workspace root is included deliberately. It is not a member of its own
    `members` globs, so a contract written only against members leaves the root
    project's own dependencies -- and the root notice file that covers them --
    unguarded.
    """
    return [SOFTWARE_ROOT, *_workspace_member_dirs()]


def _workspace_member_dirs() -> list[Path]:
    """Directories matched by `[tool.uv.workspace]`, minus its excludes.

    Resolved from the root pyproject rather than hardcoded, so a member added
    under a new glob is picked up by this contract on the same commit that
    adds it. `exclude` is expanded as a glob for the same reason `members` is:
    uv accepts a pattern in either, and treating one as a literal path would
    quietly stop excluding what it names.
    """
    workspace = _load(SOFTWARE_ROOT / "pyproject.toml").get("tool", {}).get("uv", {}).get("workspace", {})
    excluded = {path.resolve() for pattern in workspace.get("exclude", []) for path in SOFTWARE_ROOT.glob(pattern)}

    members = set()
    for pattern in workspace.get("members", []):
        for path in SOFTWARE_ROOT.glob(pattern):
            if path.is_dir() and path.resolve() not in excluded and (path / "pyproject.toml").is_file():
                members.add(path)
    return sorted(members)


def _declared_requirements(project_dir: Path) -> set[str]:
    """Normalized names of every distribution a project declares it needs.

    Runtime dependencies and extras only. Dependency groups are development
    tooling: they are not installed by a user of this tree, so nothing in them
    ships and nothing in them needs attribution.
    """
    project = _load(project_dir / "pyproject.toml").get("project", {})
    requirements = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        requirements.extend(extra)

    names = set()
    for requirement in requirements:
        match = _REQUIREMENT_NAME.match(requirement)
        if match:
            names.add(_normalize(match.group(1)))
    return names


def _workspace_distribution_names() -> set[str]:
    """The projects in this tree, which attribute themselves by shipping a LICENSE."""
    return {_normalize(_load(path / "pyproject.toml").get("project", {}).get("name", "")) for path in _project_dirs()}


def _attributed_names() -> str:
    """The notices' section headings, normalized so a name matches the way pip sees it.

    Headings rather than the whole text, because prose mentions each other
    constantly: a cross-reference like "see the `sherpa-onnx-core` section"
    would otherwise stand in for the section it points at, and this contract
    would pass on a tree where that section does not exist. Attribution is a
    section about a distribution, so a heading is what it looks like.
    """
    headings = "\n".join(
        line
        for path in _notice_files()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith("#")
    )
    return re.sub(r"[-_.]+", "-", headings).lower()


def test_every_path_named_by_notice_exists() -> None:
    entries = _notice_referenced_paths()
    missing = [entry for entry in entries if not (SOFTWARE_ROOT / entry).is_file()]

    assert entries, f"NOTICE must index the tree's {NOTICE_FILENAME} files, but no path entries were found in it"
    assert missing == [], (
        "every path listed in the root NOTICE must exist -- Apache-2.0 §4(d) makes "
        f"NOTICE part of what ships, so a dangling pointer publishes a missing attribution: {missing}"
    )


def test_every_path_named_by_notice_stays_inside_this_tree() -> None:
    # NOTICE has no file suffix, so the Markdown link contract
    # (which scans Markdown) never sees it. A path that reaches a sibling tree
    # resolves here and dangles once this tree stands alone.
    escaping = [
        entry
        for entry in _notice_referenced_paths()
        if not (SOFTWARE_ROOT / entry).resolve().is_relative_to(SOFTWARE_ROOT)
    ]

    assert escaping == [], (
        "every path listed in the root NOTICE must resolve inside this tree -- "
        f"this tree publishes on its own, so a path out of it resolves nowhere: {escaping}"
    )


def test_every_notice_file_is_indexed_by_notice() -> None:
    indexed = {(SOFTWARE_ROOT / entry).resolve() for entry in _notice_referenced_paths()}
    unindexed = [
        path.relative_to(SOFTWARE_ROOT).as_posix() for path in _notice_files() if path.resolve() not in indexed
    ]

    assert unindexed == [], (
        f"every {NOTICE_FILENAME} in this tree must be listed in the root NOTICE -- "
        f"an unlisted one is attribution that never reaches a reader of the distribution: {unindexed}"
    )


def test_every_project_has_an_indexed_notice_file() -> None:
    indexed = {(SOFTWARE_ROOT / entry).resolve() for entry in _notice_referenced_paths()}

    offenders = []
    for project_dir in _project_dirs():
        relative = project_dir.relative_to(SOFTWARE_ROOT).as_posix() or "."
        if relative in MEMBERS_WITHOUT_NOTICES:
            continue
        notice_file = project_dir / NOTICE_FILENAME
        if not notice_file.is_file():
            offenders.append(f"{relative}: no {NOTICE_FILENAME}")
        elif notice_file.resolve() not in indexed:
            offenders.append(f"{relative}: {NOTICE_FILENAME} not listed in NOTICE")

    assert offenders == [], (
        f"every project in this workspace -- the root included -- must carry a {NOTICE_FILENAME} "
        f"indexed by the root NOTICE, or be listed in MEMBERS_WITHOUT_NOTICES with a reason: {offenders}"
    )


def test_exempted_projects_declare_no_third_party_dependencies() -> None:
    # The exemption list means "has nothing to attribute", which is decidable.
    # Left to prose it becomes "nobody wrote one yet", the failure the contract
    # above exists to catch.
    internal = _workspace_distribution_names()
    offenders = [
        f"{relative}: declares {sorted(third_party)}"
        for project_dir in _project_dirs()
        if (relative := project_dir.relative_to(SOFTWARE_ROOT).as_posix() or ".") in MEMBERS_WITHOUT_NOTICES
        and (third_party := _declared_requirements(project_dir) - internal)
    ]

    assert offenders == [], (
        "a project may be exempt from carrying a notice file only while it declares no third-party "
        f"dependency; these declare one and must carry {NOTICE_FILENAME} instead: {offenders}"
    )


def test_every_declared_third_party_dependency_is_attributed() -> None:
    # The contracts above check that notice files exist and are indexed. An
    # empty one passes all of them, so this reads the text: a distribution this
    # tree declares must be named somewhere in it.
    #
    # Substring matching is deliberate -- the notices are prose, and pinning the
    # heading format would make the contract about formatting. The boundaries
    # keep `mcp` from matching inside `mcp-types`.
    attributed = _attributed_names()
    internal = _workspace_distribution_names()

    offenders = []
    for project_dir in _project_dirs():
        relative = project_dir.relative_to(SOFTWARE_ROOT).as_posix() or "."
        for name in sorted(_declared_requirements(project_dir) - internal):
            if not re.search(rf"(?<![a-z0-9-]){re.escape(name)}(?![a-z0-9-])", attributed):
                offenders.append(f"{relative} declares {name}")

    assert offenders == [], (
        "every third-party distribution declared by a project in this tree must be attributed in some "
        f"{NOTICE_FILENAME} -- an extra whose marker excludes the developing machine is attributed "
        f"nowhere and still ships on the robot: {offenders}"
    )


def test_every_relative_link_in_a_notice_file_resolves_inside_this_tree() -> None:
    offenders = []
    for path in _notice_files():
        for target in _MARKDOWN_LINK.findall(path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            resolved = (path.parent / target.split("#")[0]).resolve()
            if not resolved.exists():
                offenders.append(f"{path.relative_to(SOFTWARE_ROOT).as_posix()} -> {target} (missing)")
            elif not resolved.is_relative_to(SOFTWARE_ROOT):
                offenders.append(f"{path.relative_to(SOFTWARE_ROOT).as_posix()} -> {target} (outside this tree)")

    assert offenders == [], (
        f"every relative link in a {NOTICE_FILENAME} must resolve inside this tree -- these files "
        f"defer to each other for shared dependencies, so a broken link drops the attribution: {offenders}"
    )
