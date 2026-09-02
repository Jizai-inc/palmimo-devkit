"""Publication hygiene: content that must never ship in this repository.

This tree publishes as-is and its history is permanent: anything that lands
in a commit stays visible in clones and forks forever. This contract fails
the pull request that introduces secret material, while the mistake is
still revertible.

Only patterns that are themselves harmless to publish belong here.
"""

import codecs
import re
import subprocess
from pathlib import Path


SOFTWARE_ROOT = Path(__file__).resolve().parents[2]

BANNED_CONTENT = [
    r"TODO\(before publish\)",
    r"(^|[^0-9A-Za-z-])(i|ami|subnet|sg|vpc|eipalloc)-[0-9a-f]{8,17}([^0-9A-Za-z-]|$)",
    "AKIA[0-9A-Z]{16}",
    "BEGIN [A-Z ]*PRIVATE KEY",
]

# Scanning a binary as text does not make a file unsafe, but a chance byte
# sequence could trip a guard for no reason.
BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".pdf",
        ".glb",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".wav",
        ".mp3",
        ".mp4",
        ".zip",
        ".stl",
        ".3mf",
        ".uf2",
        ".bin",
    }
)

# Text in a multi-byte encoding interleaves its ASCII with NUL bytes, so no
# byte-level pattern can match it. Rather than silently not scanning such a
# file, its BOM makes it an offender outright: published text is UTF-8.
UNSCANNABLE_BOMS = (
    codecs.BOM_UTF16_LE,
    codecs.BOM_UTF16_BE,
    codecs.BOM_UTF32_LE,
    codecs.BOM_UTF32_BE,
)


def _banned_hits(text: str) -> list[str]:
    return [pattern for pattern in BANNED_CONTENT if re.search(pattern, text, re.MULTILINE)]


def _published_files() -> list[Path]:
    # Enumerate through git (tracked, plus new files git would accept) rather
    # than rglob'ing the working tree: only what git ships can be published,
    # so build output, tool caches, and git-ignored runtime state must not be
    # held to this contract. Paths come back relative to the cwd, so the same
    # call works from any checkout of this repository.
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=SOFTWARE_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(
        path
        for name in result.stdout.split("\0")
        if name
        # --cached keeps a deleted-but-tracked path listed; nothing to scan there.
        and (path := SOFTWARE_ROOT / name).is_file()
    )


def test_published_tree_carries_no_secret_material() -> None:
    offenders = []
    for path in _published_files():
        rel = path.relative_to(SOFTWARE_ROOT).as_posix()
        # A file can leak by name alone, so the path goes through the same
        # patterns as the bytes, for binaries too.
        for pattern in _banned_hits(rel):
            offenders.append(f"{rel} path matched /{pattern}/")
        if path.suffix in BINARY_SUFFIXES:
            continue
        data = path.read_bytes()
        if data.startswith(UNSCANNABLE_BOMS):
            offenders.append(f"{rel} is not UTF-8 (multi-byte BOM), so it cannot be scanned")
            continue
        # errors="ignore" cannot hide an ASCII pattern: dropping undecodable
        # bytes only ever pulls ASCII runs together, never apart.
        text = data.decode("utf-8", errors="ignore")
        for pattern in _banned_hits(text):
            offenders.append(f"{rel} matched /{pattern}/")

    assert offenders == [], f"Secret material must never enter the published tree: {offenders}"


def test_banned_content_scan_covers_path_names() -> None:
    # Built from pieces so the fixture itself cannot trip a content scan.
    assert _banned_hits("deploy/" + "AKIA" + "A" * 16 + ".pem")
    assert not _banned_hits("doc/reference/api-reference.md")


def test_banned_content_scan_rejects_multibyte_boms() -> None:
    assert ("AKIA" + "A" * 16).encode("utf-16").startswith(UNSCANNABLE_BOMS)
    assert not b"plain ascii".startswith(UNSCANNABLE_BOMS)
