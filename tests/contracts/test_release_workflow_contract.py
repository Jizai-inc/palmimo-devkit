"""Pins the shape of the SDK-only release workflow.

This repository's release now carries no asset -- the Palmimo Portal frontend
build moved, together with the Portal itself, to its own repository. This
contract checks the guardrails that make an
SDK-only release safe: the tag trigger, the on-main guard, draft creation
with generated notes, and the prerelease-on-hyphen rule.

Each check is scoped to the specific line or shell block that actually
carries the thing being asserted -- not "does this string appear anywhere in
the file" -- so a stray comment could not make a real drift pass silently.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release.yml"
RELEASING_GUIDE_PATH = REPO_ROOT / "doc" / "guides" / "releasing.md"

#: Matches a YAML step's ``run: |`` block: the block scalar indicator line,
#: then every following line indented deeper than it, up to (but not
#: including) the next line at the same or shallower indentation. Good
#: enough for this one workflow file's straightforward 6-space step
#: indentation -- not a general YAML parser.
_RUN_BLOCK_PATTERN = re.compile(r"^( *)run: \|\n((?:\1 .*\n?)*)", re.MULTILINE)


def _run_blocks(workflow_text: str) -> list[str]:
    """Return the body text of every ``run: |`` block in the workflow."""
    return [match.group(2) for match in _RUN_BLOCK_PATTERN.finditer(workflow_text)]


def test_release_workflow_triggers_on_version_tags() -> None:
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))

    # PyYAML parses the bare `on:` key as the boolean `True`.
    on_section = workflow.get("on") or workflow.get(True)
    assert on_section is not None, f"{WORKFLOW_PATH} has no 'on:' trigger section"
    tags = on_section.get("push", {}).get("tags", [])
    assert "v*" in tags, f"{WORKFLOW_PATH} must trigger on 'v*' tag pushes, got {tags!r}"


def test_release_workflow_refuses_a_tag_off_main() -> None:
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    blocks = _run_blocks(text)
    guard_blocks = [block for block in blocks if "merge-base" in block and "--is-ancestor" in block]
    assert guard_blocks, f"{WORKFLOW_PATH} must verify the tagged commit is an ancestor of main"
    assert any("origin/main" in block for block in guard_blocks), (
        f"{WORKFLOW_PATH}'s on-main guard must check ancestry against origin/main"
    )


def test_release_workflow_refuses_to_touch_a_published_release() -> None:
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    blocks = _run_blocks(text)
    guard_blocks = [block for block in blocks if "isDraft" in block]
    assert guard_blocks, f"{WORKFLOW_PATH} must check whether an existing release is a draft before touching it"
    assert any("already published" in block for block in guard_blocks), (
        f"{WORKFLOW_PATH} must refuse to modify an already-published release"
    )


def test_release_workflow_creates_a_draft_with_generated_notes() -> None:
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    blocks = _run_blocks(text)
    create_blocks = [block for block in blocks if "gh release create" in block]
    assert create_blocks, f"{WORKFLOW_PATH} has no 'gh release create' run block to check"
    assert any("--draft" in block and "--generate-notes" in block for block in create_blocks), (
        f"{WORKFLOW_PATH}'s 'gh release create' invocation must use --draft --generate-notes"
    )


def test_release_workflow_marks_a_hyphenated_tag_as_a_prerelease() -> None:
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    blocks = _run_blocks(text)
    create_blocks = [block for block in blocks if "gh release create" in block]
    assert create_blocks, f"{WORKFLOW_PATH} has no 'gh release create' run block to check"
    assert any("--prerelease" in block for block in create_blocks), (
        f"{WORKFLOW_PATH} must pass --prerelease for a tag, so a pre-release build can never surface as releases/latest"
    )
    assert any(re.search(r"\*-\*\)", block) for block in create_blocks), (
        f"{WORKFLOW_PATH} must gate --prerelease on the tag name containing a '-' "
        '(e.g. via a `case "$TAG" in *-*) ...` shell pattern)'
    )


def test_release_workflow_never_references_the_portal_package_path() -> None:
    # The Portal (and its frontend build) moved to its own repository. A
    # passing mention that it used to live here / moved elsewhere is fine
    # (see the header comment); a reference to its package path or build
    # tooling is not -- that would mean asset-building logic crept back in.
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "packages/palmimo_portal" not in text
    assert "palmimo-portal-static" not in text


def test_release_workflow_has_no_asset_build_or_upload_steps() -> None:
    # An SDK-only release carries no asset: no Node setup, no frontend
    # build, no tarball/checksum packaging, no asset upload.
    assert WORKFLOW_PATH.is_file(), f"missing {WORKFLOW_PATH}"
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "setup-node" not in text
    assert "npm ci" not in text
    assert "sha256sum" not in text
    assert "gh release upload" not in text
    assert "tar -c" not in text


def test_releasing_guide_documents_the_prerelease_rule() -> None:
    assert RELEASING_GUIDE_PATH.is_file(), f"missing {RELEASING_GUIDE_PATH}"
    text = RELEASING_GUIDE_PATH.read_text(encoding="utf-8")
    assert "prerelease" in text.lower() or "pre-release" in text.lower(), (
        f"{RELEASING_GUIDE_PATH} must document the pre-release-on-hyphen rule"
    )


def test_releasing_guide_never_references_the_portal_package() -> None:
    assert RELEASING_GUIDE_PATH.is_file(), f"missing {RELEASING_GUIDE_PATH}"
    text = RELEASING_GUIDE_PATH.read_text(encoding="utf-8")
    # The guide is allowed to say Portal moved to its own repository -- but
    # never to describe a release asset, a device update flow, or anything
    # else specific to the Portal package that used to live here.
    assert "palmimo-portal-static" not in text
    assert "Updater" not in text
