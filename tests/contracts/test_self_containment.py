"""Self-containment contract: the published tree must not lean on lerobot.

This tree ships on its own as the public repository, so its declared
dependencies must resolve entirely from PyPI plus this tree's own uv
workspace — nothing may point at `lerobot` or at a source outside the
workspace. The `integrations/lerobot/` plugins sit under this tree and are
published with it, but they form their own uv workspace and depend on
`lerobot` one-way; what this contract forbids is that dependency reaching the
resolution that ships here.

The contract is asserted on the declared dependencies, `[tool.uv.sources]`
entries, and the resolved lockfile — independent of import timing, so it
holds even against a lazily imported dependency that a runtime import check
could not see.

Which projects those assertions cover is derived from `[tool.uv.workspace]`
rather than restated here, so "is a workspace member" implies "is scanned" by
construction: whatever reaches the root `uv.lock` has been read by the checks
below. A project outside the workspace still ships in the published tree, so
it must be either scanned or recorded in `UNSCANNED_PROJECTS` with the reason
it is exempt. Without that, a nested project — a separate workspace parked
under this tree — would sit in the published repository with none of these
checks ever looking at it.
"""

import subprocess
import tomllib
from pathlib import Path


SOFTWARE_ROOT = Path(__file__).resolve().parents[2]

# Projects in this tree that the checks below deliberately skip, mapped to
# the reason each is exempt. Keys are root-relative posix paths to the
# pyproject.toml itself, e.g. "integrations/lerobot/pyproject.toml".
#
# A project outside the uv workspace keeps test_no_pyproject_escapes_the_contract
# red until it is either brought into the workspace or recorded here, so the
# exemption is argued in review rather than acquired by being new. A workspace
# member can never be listed here -- its dependencies resolve into
# the root uv.lock, so it has to be scanned.
#
# The three entries below are the LeRobot integration, which is exempt for the
# one reason this module exists to enforce: it depends on `lerobot`, and the
# whole point of the checks here is that nothing resolving into this tree's
# uv.lock may. Making it a workspace member would pull `lerobot` -- and through
# it draccus -> pyyaml-include (GPLv3+) -- into the root uv.lock, which is the
# set the shipped Pi image installs. It is instead its own uv workspace with its
# own uv.lock, its own lint/type/test settings, and its own CI job
# (`integration-lerobot`), so "exempt from these checks" is not "unchecked".
#
# It ships in the published repository as its own workspace, outside the
# root uv.lock.
_LEROBOT_EXEMPTION = (
    "separate uv workspace: it depends on lerobot one-way, which is exactly "
    "what this tree's self-containment forbids, so it cannot be a member here. "
    "Covered by its own uv.lock and the integration-lerobot CI job."
)

UNSCANNED_PROJECTS: dict[str, str] = dict.fromkeys(
    (
        "integrations/lerobot/pyproject.toml",
        "integrations/lerobot/lerobot_robot_palmimo/pyproject.toml",
        "integrations/lerobot/lerobot_teleoperator_palmimo/pyproject.toml",
    ),
    _LEROBOT_EXEMPTION,
)

# An exemption has to say something a reviewer can weigh, not "n/a".
MINIMUM_REASON_LENGTH = 20


def _load(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def _workspace_member_dirs() -> list[Path]:
    """Directories the root pyproject declares as uv workspace members.

    Expanded from ``[tool.uv.workspace]`` against the filesystem because that
    is what uv itself does: these globs decide what resolves into
    the root ``uv.lock``, so deriving the scanned set from them keeps "locked"
    and "checked" the same set rather than two hand-maintained lists that can
    drift apart. A glob match without a pyproject.toml is not a project
    (``examples/*`` also matches the ``examples/agents`` grouping directory and
    ``examples/README.md``), and ``exclude`` wins over ``members``.
    """
    workspace = _load(SOFTWARE_ROOT / "pyproject.toml").get("tool", {}).get("uv", {}).get("workspace", {})
    excluded = {path.resolve() for pattern in workspace.get("exclude", []) for path in SOFTWARE_ROOT.glob(pattern)}
    members = {
        path
        for pattern in workspace.get("members", [])
        for path in SOFTWARE_ROOT.glob(pattern)
        if path.is_dir() and path.resolve() not in excluded and (path / "pyproject.toml").is_file()
    }
    return sorted(members)


PYPROJECT_FILES = [SOFTWARE_ROOT / "pyproject.toml", *(d / "pyproject.toml" for d in _workspace_member_dirs())]


def _every_pyproject_file() -> list[Path]:
    """Every pyproject.toml git would ship from this tree, scanned or not.

    The counterpart to ``_workspace_member_dirs``: that one collects what the
    contract covers, this one collects what exists, and the difference is what
    nothing is checking.

    Enumerated through git rather than by walking the working tree, matching
    the sibling contracts in this directory. Only what git carries can reach
    the published repository, and a git-ignored path holds local or runtime
    state this tree's contracts do not govern -- ``.venv/`` and ``build/``, but
    also ``examples/agents/openclaw/volumes/``, which OpenClaw fills with
    third-party content at runtime. Walking the filesystem instead would hold
    those to this tree's rules and turn a developer's machine red for something
    the repository does not contain. ``--others`` keeps files that are
    untracked but not ignored, so a project added and not yet committed still
    counts -- catching it before it lands is the entire point.
    """
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "*pyproject.toml"],
        cwd=SOFTWARE_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    paths = (SOFTWARE_ROOT / name for name in result.stdout.split("\0") if name)
    return sorted(path for path in paths if path.is_file())


def _relative(paths: list[Path]) -> set[str]:
    return {path.relative_to(SOFTWARE_ROOT).as_posix() for path in paths}


def _package_name(dependency: str) -> str:
    """First token of a PEP 508 dependency string, lower-cased.

    Strips version specifiers, extras (``pkg[extra]``), and environment
    markers, leaving just the distribution name for a case-insensitive
    comparison (``Lerobot`` and ``lerobot`` are the same package).
    """
    name = dependency.split(";")[0].strip()
    for sep in ("[", "=", "<", ">", "!", "~", "@", " "):
        idx = name.find(sep)
        if idx != -1:
            name = name[:idx]
    return name.strip().lower()


def _dependency_entries(project: dict) -> list[str]:
    entries = list(project.get("project", {}).get("dependencies", []))
    for group_deps in project.get("project", {}).get("optional-dependencies", {}).values():
        entries.extend(group_deps)
    # PEP 735 dependency groups and uv's legacy dev-dependencies also resolve into the lockfile.
    for group_deps in project.get("dependency-groups", {}).values():
        entries.extend(d for d in group_deps if isinstance(d, str))
    entries.extend(project.get("tool", {}).get("uv", {}).get("dev-dependencies", []))
    return entries


def test_no_pyproject_declares_a_lerobot_dependency() -> None:
    offenders = [
        f"{path.relative_to(SOFTWARE_ROOT).as_posix()}: {entry}"
        for path in PYPROJECT_FILES
        for entry in _dependency_entries(_load(path))
        if _package_name(entry) == "lerobot"
    ]

    assert offenders == [], (
        "This tree is the published tree and must declare no dependency on "
        f"lerobot (that pin belongs only to integrations/lerobot/): {offenders}"
    )


def test_every_uv_source_points_at_a_workspace_member() -> None:
    offenders = []
    for path in PYPROJECT_FILES:
        sources = _load(path).get("tool", {}).get("uv", {}).get("sources", {})
        for name, table in sources.items():
            if table != {"workspace": True}:
                offenders.append(f"{path.relative_to(SOFTWARE_ROOT).as_posix()}: {name} = {table}")

    assert offenders == [], (
        "every [tool.uv.sources] entry in this tree must resolve to a workspace "
        f"member ({{ workspace = true }}) — a git/url/path source would reach "
        f"outside this self-contained tree: {offenders}"
    )


def test_pyproject_files_include_nested_example_projects() -> None:
    """The member globs must reach a project nested below a grouping directory.

    ``examples/agents/wakeword`` sits two levels under ``examples/`` and is
    matched by its own ``examples/agents/*`` glob rather than by ``examples/*``,
    so dropping that glob would silently shrink what the contract reads.
    """
    wakeword_pyproject = SOFTWARE_ROOT / "examples" / "agents" / "wakeword" / "pyproject.toml"
    assert wakeword_pyproject in PYPROJECT_FILES, (
        "the [tool.uv.workspace] member globs must reach a project nested one "
        "level below a grouping directory (examples/agents/wakeword/), not just "
        f"direct children: {PYPROJECT_FILES}"
    )


def test_the_scan_finds_this_trees_own_pyproject() -> None:
    """A scan that finds nothing would make the guards below pass vacuously.

    ``test_no_pyproject_escapes_the_contract`` compares set differences and the
    stale check iterates an empty mapping, so an enumeration that quietly
    returns nothing — git absent, a wrong working directory, a pathspec that
    stops matching — disarms both without turning anything red. Fail loudly
    here instead of reporting all-clear on an empty scan.
    """
    assert SOFTWARE_ROOT / "pyproject.toml" in _every_pyproject_file(), (
        "git enumerated no pyproject.toml under this tree, not even the root "
        "one — the scan behind the guards below is broken, not the tree it scans"
    )


def test_no_pyproject_escapes_the_contract() -> None:
    """Every project in this tree is either scanned above or exempt on record.

    The checks in this module read the uv workspace, so a project parked
    outside it — its own workspace, say — would ship in the published
    repository without a single one of them looking at it. Widening the
    workspace is not always right (a separate workspace legitimately declares
    dependencies this tree may not), so the contract is that the choice gets
    made, not that it goes one way.
    """
    escaped = sorted(_relative(_every_pyproject_file()) - _relative(PYPROJECT_FILES) - set(UNSCANNED_PROJECTS))

    assert escaped == [], (
        "these projects live in this tree — the one that ships as the public "
        "repository — but no self-containment check reads them. Bring them into "
        "the uv workspace to put them under the contract, or record them in "
        f"UNSCANNED_PROJECTS with the reason they are exempt: {escaped}"
    )


def test_no_unscanned_project_is_a_workspace_member() -> None:
    """Exemption is only available to projects outside the locked resolution.

    A workspace member's dependencies resolve into the root ``uv.lock``, so
    exempting one would park an unread project inside what already ships.
    Deriving PYPROJECT_FILES from the member globs makes that impossible to do
    by accident; this catches doing it deliberately.
    """
    overlap = sorted(set(UNSCANNED_PROJECTS) & _relative(PYPROJECT_FILES))

    assert overlap == [], (
        "these projects are uv workspace members, so their dependencies resolve "
        "into the root uv.lock and they must be scanned — UNSCANNED_PROJECTS is "
        f"only for projects outside the workspace: {overlap}"
    )


def test_every_unscanned_project_records_a_reason() -> None:
    """The mapping's values are the point: an exemption has to be argued.

    A bare list would let a path be added silently. Nothing else reads these
    strings, so without this check the obligation to justify an exemption would
    live only in prose that no build step enforces.
    """
    unexplained = sorted(
        path for path, reason in UNSCANNED_PROJECTS.items() if len(reason.strip()) < MINIMUM_REASON_LENGTH
    )

    assert unexplained == [], (
        "every UNSCANNED_PROJECTS entry needs a reason a reviewer can weigh — at "
        f"least {MINIMUM_REASON_LENGTH} characters saying why the project is "
        f"exempt, not a placeholder: {unexplained}"
    )


def test_no_unscanned_project_entry_is_stale() -> None:
    """An exemption outlives the project it was written for unless something checks.

    A stale entry is worse than none: it silently pre-approves whatever later
    takes that path.
    """
    existing = _relative(_every_pyproject_file())
    stale = sorted(path for path in UNSCANNED_PROJECTS if path not in existing)

    assert stale == [], (
        "UNSCANNED_PROJECTS exempts paths that no longer hold a pyproject.toml — "
        f"drop them so the list stays an inventory of real decisions: {stale}"
    )


def test_the_lockfile_resolves_no_lerobot_package() -> None:
    lock = _load(SOFTWARE_ROOT / "uv.lock")
    offenders = [pkg.get("name", "") for pkg in lock.get("package", []) if pkg.get("name", "").lower() == "lerobot"]

    assert offenders == [], (
        "The root uv.lock must resolve no lerobot package — this is the only check "
        "that catches a transitive reintroduction (lerobot pulled in by some other "
        f"dependency) rather than a direct declaration: {offenders}"
    )


def test_the_lockfile_resolves_no_gpl_distance_package() -> None:
    """``distance`` (0.1.3, GPL-2.0) is a declared dependency of g2p-en, which
    piper-plus pulls in for the speech extra — but current g2p-en never imports
    it (dead, pre-2013 edit-distance utility). It is dropped via a
    ``[[tool.uv.dependency-metadata]]`` override on g2p-en in pyproject.toml
    (its declared deps replaced with the ones it actually uses) so it never
    resolves into this GPL-clean tree's lockfile.
    """
    lock = _load(SOFTWARE_ROOT / "uv.lock")
    offenders = [pkg.get("name", "") for pkg in lock.get("package", []) if pkg.get("name", "").lower() == "distance"]

    assert offenders == [], (
        "The root uv.lock must resolve no distance package (GPL-2.0, unused "
        f"transitive dep of g2p-en) — see [[tool.uv.dependency-metadata]] for g2p-en: {offenders}"
    )
