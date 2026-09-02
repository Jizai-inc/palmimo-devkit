"""Ratchets against documentation claims this tree has already corrected.

Each contract here pins one statement that was wrong once and is expensive to
notice again by eye: prose describing behaviour the code never had, or an
ignore rule that keeps a multi-megabyte download out of a commit. Everything
scanned lives inside this tree, so the suite runs unchanged wherever the
tree is checked out.
"""

import subprocess
from pathlib import Path


SOFTWARE_ROOT = Path(__file__).resolve().parents[2]
README = SOFTWARE_ROOT / "README.md"
GITIGNORE = SOFTWARE_ROOT / ".gitignore"

# `palmimo_sdk.mcp` keeps an unreachable peripheral's tools listed and answers
# a call with a not-attached message; it never removed them from tools/list.
RETIRED_PERIPHERAL_CLAIM = "disables the matching tools"


def _tracked_markdown_files() -> list[Path]:
    # Enumerate through git so only files that actually ship are scanned, and
    # so an empty result fails loudly instead of passing vacuously.
    result = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=SOFTWARE_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    files = sorted(SOFTWARE_ROOT / line for line in result.stdout.splitlines() if line)
    assert files, "git ls-files found no tracked markdown files in this tree"
    return files


def test_markdown_omits_the_retired_peripheral_disable_claim() -> None:
    offenders = [
        path.relative_to(SOFTWARE_ROOT).as_posix()
        for path in _tracked_markdown_files()
        if RETIRED_PERIPHERAL_CLAIM in path.read_text(encoding="utf-8")
    ]

    assert offenders == [], (
        f"{RETIRED_PERIPHERAL_CLAIM!r} describes behaviour the MCP server does not have -- "
        f"an unreachable peripheral keeps its tools listed and returns a not-attached "
        f"message instead: {offenders}"
    )


def test_gitignore_covers_downloaded_voice_models() -> None:
    patterns = {line.strip() for line in GITIGNORE.read_text(encoding="utf-8").splitlines()}

    missing = [
        pattern for pattern in ("/*.onnx", "/*.onnx.json", "/*.onnx.ok", "/config.json") if pattern not in patterns
    ]

    assert missing == [], (
        f"The SDK caches voice models outside this tree, but `piper --download-model` run by hand "
        f"still defaults to the current directory: it drops a large .onnx into the repository root, "
        f"plus its config as either config.json or <name>.onnx.json depending on the catalogue it "
        f"came from, and loading it there adds piper's optimized copy and its .ok sentinel; keep "
        f"every shape ignored so none of them land in a commit: {missing}"
    )
