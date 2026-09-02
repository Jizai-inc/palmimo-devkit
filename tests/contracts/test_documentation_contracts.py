"""Documentation placement contracts for this tree.

Every contract here is scoped to the tree this test suite ships with: scan
roots stay inside it, so the suite runs unchanged wherever the tree lives.
"""

import re
import subprocess
import unicodedata
from functools import cache
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STRAY_COMMAND = re.compile(r"\buv\s+(?:run|sync|add)\b|\bcd\s+software\b", re.IGNORECASE)
# Captures a link/image target up to the first ')' or '#', so in-page anchors
# (`file.md#section`) are dropped from the match automatically.
LINK_TARGET = re.compile(r"\]\(([^)#]+)")
# HTML asset/link targets Markdown syntax doesn't cover, e.g. `<img src="...">`.
# Any element, not a list of the ones seen so far: the site splash also reaches
# for assets through a custom element, and an allow-list would have let that one
# through unchecked. A route the build generates rather than the tree carrying it
# is named in frontmatter instead, where sync-doc.mjs is the check.
HTML_LINK_TARGET = re.compile(r'<[a-zA-Z][\w-]*\b[^>]*?\s(?:src|href)="([^"#]+)"')
# Reference-style link definitions: `[label]: target`.
REFERENCE_LINK_TARGET = re.compile(r"^\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
# The `#anchor` counterpart LINK_TARGET deliberately drops -- cross-file
# (`path#anchor`) or same-file (`#anchor`, empty path group). HTML and
# reference-style links carry no anchors anywhere in this tree, so only the
# `](...)` syntax needs an anchor-capturing counterpart.
ANCHOR_LINK_TARGET = re.compile(r"\]\(([^)#]*)#([^)\s]+)\)")
# ATX headings; GitHub strips an optional trailing `#…#` closing sequence.
HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
# A fenced code block, so a `#` comment inside a code sample is never
# mistaken for a heading. Mirrors FENCED_CODE in test_comment_language.py.
FENCED_CODE = re.compile(r"^[ \t]*(```|~~~).*?(^[ \t]*\1|\Z)", re.DOTALL | re.MULTILINE)
# A backtick code span, so a link form quoted inside prose is read as the sample
# it is. Mirrors INLINE_CODE in test_comment_language.py.
INLINE_CODE = re.compile(r"`[^`\n]*`")
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPTS_README = SCRIPTS_DIR / "README.md"
DOC_ROOT = REPO_ROOT / "doc"
# The documentation site is a rendering of doc/, so a site route
# (`/guides/installation/`) names the doc/ page behind it rather than a file
# of the site's own. Only the splash is written by hand; the directory the
# render lands in holds no tracked page at all.
SITE_PAGE_SOURCES = REPO_ROOT / "docs-site" / "src" / "site-pages"
SITE_SPLASH = SITE_PAGE_SOURCES / "index.mdx"
SITE_RENDER_DIR = REPO_ROOT / "docs-site" / "src" / "content" / "docs"
SITE_PUBLIC_DIR = REPO_ROOT / "docs-site" / "public"
# A path segment that is already the slug Astro would generate for it.
SITE_SLUG_SEGMENT = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# A leftover reference to the pre-migration subdirectory this repository's
# root used to be nested under, or a `cd` into that old directory (see the
# restructuring that flattened it to the root). Matched as a path segment --
# preceded by a separator, followed by one, or both -- plus the `cd` form,
# treating both `/` and `\` as separators. A negative lookaround on the far
# side rules out a hyphenated compound reading as the bare name (an asset
# could plausibly be named e.g. `software-layers.drawio.svg`).
#
# A bare mention with no separator on either side, e.g. "the software
# directory", is deliberately left alone: ordinary prose ("the software
# stack", "software SDK rules", both of which this tree legitimately uses)
# reads identically, and without a separator there is no path-shaped signal
# to tell the two apart.
#
# Referenced through this variable rather than spelled out directly against
# a separator or after `cd `, so this definition does not itself trip the
# check it defines -- do not paste the bare word directly next to `/`, `\`,
# or after `cd ` elsewhere in this file either; it would trip this same
# check the next time this test runs.
_STALE_SUBDIR = "software"
_SEPARATOR = r"[\\/]"
STALE_PATH_REFERENCE = re.compile(
    rf"(?<={_SEPARATOR}){_STALE_SUBDIR}(?![\w-])"
    rf"|(?<![\w-]){_STALE_SUBDIR}(?={_SEPARATOR})"
    rf"|\bcd\s+{_STALE_SUBDIR}\b"
)
# The Raspberry Pi Imager download page's URL happens to end in a path
# segment spelled the same way -- not a reference to this repository. Built
# the same indirect way as the pattern above, and matched by exact URL
# rather than by domain alone, so the exemption only swallows that one
# span of a line and not a genuine offender that happens to share the line.
RASPBERRY_PI_IMAGER_URL = re.compile(rf"https://www\.raspberrypi\.com/{_STALE_SUBDIR}/")

# Markdown under the repository that the placement contracts scan. Kept in one
# place so both contracts cover the same files.
MARKDOWN_SCAN_ROOTS = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "doc",
    REPO_ROOT / "docs-site",
    REPO_ROOT / "examples",
    REPO_ROOT / "integrations",
    REPO_ROOT / "packages",
    REPO_ROOT / "scripts",
)

# The pages allowed to carry runnable commands. The rule is one home per
# procedure, not one file per tree: a page belongs here when it is where the
# reader is already standing when they need the command -- a guide they are
# following, the README of the project they are installing, the contributor
# loop. Everything else links here instead, which is why reference pages that
# only state facts, explanation pages, and the index pages that route between
# them are absent: a procedure restated in two places rots in one of them.
#
# Adding an entry is a decision about where a procedure lives, so it is made
# here in review rather than by dropping commands into a file and finding the
# contract silent.
COMMAND_PAGES = frozenset(
    {
        "README.md",
        "CONTRIBUTING.md",
        "doc/guides/controlling-motions.md",
        "doc/guides/installation.md",
        "doc/guides/mcp-server.md",
        "doc/guides/motion-development-guide.md",
        "doc/guides/raspberry-pi-setup.md",
        "doc/guides/releasing.md",
        "docs-site/README.md",
        "examples/agents/companion/README.md",
        "examples/agents/openclaw/README.md",
        "examples/agents/wakeword/README.md",
        # Its own uv workspace, resolved separately from this one, so its
        # install and run commands cannot be stated from here.
        "integrations/lerobot/README.md",
        "scripts/README.md",
    }
)


def _markdown_files(*roots: Path) -> list[Path]:
    # Enumerate through git (tracked, plus new files git would accept) rather
    # than rglob'ing the working tree: only what git ships can reach the
    # published mirror, and runtime state written under a git-ignored path
    # (e.g. the openclaw kit's volumes/, which OpenClaw fills with third-party
    # plugin markdown at runtime) must not be held to this tree's contracts.
    # Same shipped-file rationale as _shipped_files in test_comment_language.py.
    for root in roots:
        # A stale root would silently narrow the contract; fail loudly instead.
        assert root.exists(), f"Documentation scan root is missing: {root}"
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    files = {REPO_ROOT / name for name in result.stdout.split("\0") if name}
    selected = sorted(
        path
        for path in files
        if any(path == root or root in path.parents for root in roots)
        # --cached keeps a deleted-but-tracked path listed; nothing to scan there.
        and path.is_file()
        and path.name != "AGENTS.md"
    )
    assert selected, "git enumerated no markdown files under the documentation scan roots"
    return selected


def test_every_command_page_exists() -> None:
    # A renamed page would leave a dead entry behind, and the contract below
    # would go on passing while the commands it was meant to place moved
    # somewhere nobody declared.
    missing = sorted(page for page in COMMAND_PAGES if not (REPO_ROOT / page).is_file())

    assert missing == [], f"COMMAND_PAGES names pages this tree does not have: {missing}"


def test_commands_live_on_their_documented_page() -> None:
    candidates = _markdown_files(*MARKDOWN_SCAN_ROOTS)
    offenders = [
        relative
        for path in candidates
        if (relative := path.relative_to(REPO_ROOT).as_posix()) not in COMMAND_PAGES
        and STRAY_COMMAND.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], (
        f"These pages state commands they are not the home of: {offenders}. Link to the page that "
        f"owns the procedure, or add this page to COMMAND_PAGES if the procedure belongs here."
    )


def _git_tracked_markdown_files() -> list[Path]:
    # git ls-files enumerates only what is actually tracked, so an untracked
    # scratch file can't join this contract, and a scan that returned nothing
    # would fail loudly below instead of passing vacuously.
    # `*.mdx` as well as `*.md`: the splash reaches for a component, so it is
    # MDX, and a scan that read only Markdown would stop covering it in silence.
    result = subprocess.run(
        ["git", "ls-files", "*.md", "*.mdx"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    files = sorted(REPO_ROOT / line for line in result.stdout.splitlines() if line)
    assert files, "git ls-files found no tracked markdown files under this repository"
    return files


def _link_targets(text: str) -> list[str]:
    return LINK_TARGET.findall(text) + HTML_LINK_TARGET.findall(text) + REFERENCE_LINK_TARGET.findall(text)


def _doc_source_for_site_route(page: Path, target: str) -> Path | None:
    """Resolve a documentation-site route to the file rendered into it.

    Returns ``None`` when the target is not a site route, leaving it to the
    filesystem resolution the rest of the tree uses.
    """
    if not target.startswith("/") or not page.is_relative_to(SITE_PAGE_SOURCES):
        return None
    route = target.strip("/")
    if not route:
        return SITE_SPLASH
    # /images/ is rendered out of doc/images/, so the tree's copy of a diagram
    # is there and not in the generated public/images/.
    if route.startswith("images/"):
        return DOC_ROOT / route
    # Any other route naming a file is an asset the site publishes verbatim out
    # of its own public/ -- the 3D model, the photography. A route naming no
    # file is a page, and takes the route its path in doc/ spells out.
    if Path(route).suffix:
        return SITE_PUBLIC_DIR / route
    return DOC_ROOT / f"{route}.md"


def test_markdown_links_resolve_within_the_tree() -> None:
    # Only this tree ships in the published mirror, so a relative link/image
    # target must both resolve to a real file and stay inside this tree.
    # http(s) and mailto targets, and in-page anchors, are out of scope.
    offenders = []
    for path in _git_tracked_markdown_files():
        text = path.read_text(encoding="utf-8")
        for raw_target in _link_targets(text):
            target = raw_target.strip()
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue

            site_page = _doc_source_for_site_route(path, target)
            resolved = site_page if site_page is not None else (path.parent / target).resolve()
            relative_path = path.relative_to(REPO_ROOT).as_posix()
            try:
                resolved.relative_to(REPO_ROOT)
            except ValueError:
                offenders.append(f"{relative_path} -> {target} (escapes the tree)")
                continue

            if not resolved.exists():
                offenders.append(f"{relative_path} -> {target} (target does not exist)")

    assert offenders == [], f"Fix broken or out-of-tree markdown links: {offenders}"


def test_doc_page_paths_are_already_site_slugs() -> None:
    # A site route is the doc/ path with its extension dropped, which the
    # contract above reads backwards to find the page. That inverse only holds
    # while each segment is already its own slug: `doc/guides/My Guide.md`
    # would be served at a route this contract could not resolve.
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in _git_tracked_markdown_files()
        if path.is_relative_to(DOC_ROOT)
        and not all(
            SITE_SLUG_SEGMENT.fullmatch(segment) for segment in path.relative_to(DOC_ROOT).with_suffix("").parts
        )
    ]

    assert offenders == [], (
        f"These doc/ pages take a site route that cannot be read back to them: {offenders}. "
        f"Name a page in lowercase words joined by hyphens."
    )


def test_doc_pages_link_in_the_form_the_site_can_rewrite() -> None:
    # sync-doc.mjs rewrites `[text](target)` and nothing else. A reference-style
    # link or a raw <a href> survives into the rendered page untouched, so a
    # target that escapes doc/ -- which has to become a GitHub blob URL to
    # resolve at all -- ships as a relative link the site cannot follow. The
    # link contract above reads all three forms, so the break is invisible
    # there too: it resolves on disk and 404s on the site.
    offenders = []
    for path in _git_tracked_markdown_files():
        if not path.is_relative_to(DOC_ROOT):
            continue
        text = INLINE_CODE.sub("", FENCED_CODE.sub("", path.read_text(encoding="utf-8")))
        relative_path = path.relative_to(REPO_ROOT).as_posix()
        for target in REFERENCE_LINK_TARGET.findall(text):
            offenders.append(f"{relative_path} -> {target} (reference-style link)")
        for target in HTML_LINK_TARGET.findall(text):
            offenders.append(f"{relative_path} -> {target} (raw HTML link)")

    assert offenders == [], (
        f"These doc/ links are invisible to the site's rewriter: {offenders}. Write them as [text](target)."
    )


def test_the_site_hosts_no_hand_written_page_beside_the_splash() -> None:
    # doc/ is the only home for the documentation's text; the site renders it.
    # A second hand-written page here is a second home for whatever it says,
    # and the copy that is not read is the one that goes stale unnoticed.
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in _git_tracked_markdown_files()
        if (path.is_relative_to(SITE_PAGE_SOURCES) and path != SITE_SPLASH) or path.is_relative_to(SITE_RENDER_DIR)
    ]

    assert offenders == [], (
        f"These pages are written by hand inside the documentation site: {offenders}. "
        f"Site pages are rendered from doc/ -- add the page there instead."
    )


def _github_slug(heading_text: str) -> str:
    # GitHub's rule: lowercase, drop everything but hyphens, spaces, and what
    # Ruby's \p{Word} keeps -- letters, marks, decimal digits, and connector
    # punctuation (removed, not replaced) -- then turn each remaining space
    # into a hyphen; consecutive spaces become consecutive hyphens, never
    # collapsed. Marks matter: an emoji written with a variation selector
    # (U+26A0 U+FE0F) leaves that selector in the anchor, and Python's \w
    # would drop it and report a match GitHub does not make.
    slug = heading_text.strip().lower()
    kept = "".join(ch for ch in slug if ch in "- " or _is_github_word_character(ch))
    return kept.replace(" ", "-")


def _is_github_word_character(char: str) -> bool:
    category = unicodedata.category(char)
    return category[0] in ("L", "M") or category in ("Nd", "Pc")


@cache
def _anchors_in_file(path: Path) -> frozenset[str]:
    text = FENCED_CODE.sub("", path.read_text(encoding="utf-8"))
    seen: dict[str, int] = {}
    anchors = set()
    for _, heading_text in HEADING.findall(text):
        slug = _github_slug(heading_text)
        count = seen.get(slug, 0)
        anchors.add(slug if count == 0 else f"{slug}-{count}")
        seen[slug] = count + 1
    return frozenset(anchors)


def test_markdown_anchor_links_resolve_to_headings() -> None:
    # A link/image target's file existing (test_markdown_links_resolve_within_the_tree)
    # doesn't mean its `#anchor` still matches a heading -- renaming or moving
    # a heading silently breaks every link into it.
    offenders = []
    checked = 0
    for path in _git_tracked_markdown_files():
        text = path.read_text(encoding="utf-8")
        for raw_path, anchor in ANCHOR_LINK_TARGET.findall(text):
            target = raw_path.strip()
            site_page = _doc_source_for_site_route(path, target)
            if site_page is not None:
                target_path = site_page
            else:
                target_path = path if not target else (path.parent / target).resolve()
            try:
                target_path.relative_to(REPO_ROOT)
            except ValueError:
                continue  # out-of-tree target -- the link-resolution test already flags it

            if target_path.suffix != ".md" or not target_path.is_file():
                continue  # missing/non-markdown target -- already flagged elsewhere

            checked += 1
            relative_path = path.relative_to(REPO_ROOT).as_posix()
            if anchor not in _anchors_in_file(target_path):
                target_relative = target_path.relative_to(REPO_ROOT).as_posix()
                offenders.append(f"{relative_path} -> {target_relative}#{anchor} (no matching heading)")

    assert checked > 0, "no markdown anchor links found to check"
    assert offenders == [], f"Fix markdown anchor links that don't match a heading: {offenders}"


def test_markdown_uses_assets_instead_of_box_style_ascii_diagrams() -> None:
    # Diagrams belong in .drawio.svg assets. Only box-style frames (top-left
    # corner char) are banned here; directory trees drawn with tee/vertical
    # characters are allowed.
    #
    # Scanned across the whole tree rather than MARKDOWN_SCAN_ROOTS, which
    # scopes the command contract: this rule applies wherever Markdown ships,
    # with nothing to exempt.
    candidates = _git_tracked_markdown_files()
    offenders = [
        path.relative_to(REPO_ROOT).as_posix() for path in candidates if "┌" in path.read_text(encoding="utf-8")
    ]

    assert offenders == [], f"Replace ASCII diagrams with .drawio.svg assets: {offenders}"


def test_every_user_script_is_documented_in_both_readmes() -> None:
    # A script that ships without a mention in either README is undiscoverable.
    # Direct children of scripts/ are the user-facing CLI surface (nested
    # subpackages are private implementation — see test_repository_conventions);
    # each must at least be named in README.md and scripts/README.md.
    assert SCRIPTS_DIR.is_dir(), f"Scripts directory is missing: {SCRIPTS_DIR}"
    names = sorted(path.name for path in SCRIPTS_DIR.glob("*.py"))
    assert names != [], f"No user scripts found under {SCRIPTS_DIR}"

    main_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    scripts_readme = SCRIPTS_README.read_text(encoding="utf-8")
    missing_in_main = [name for name in names if name not in main_readme]
    missing_in_scripts = [name for name in names if name not in scripts_readme]

    assert missing_in_main == [], f"Name these scripts in README.md's documentation index: {missing_in_main}"
    assert missing_in_scripts == [], f"Document these scripts in scripts/README.md: {missing_in_scripts}"


def test_markdown_and_contracts_avoid_stale_software_paths() -> None:
    # This repository's root used to be nested one level down, under a
    # subdirectory named after that old layout; that migration is what
    # flattened this contracts module up to its current location. A path or
    # `cd` naming that old subdirectory is either a leftover the migration
    # missed or a regression back toward it, in a Markdown page or in this
    # very module (the one place that spells the old name out, to define the
    # check below) -- either way, this repository's root already IS the
    # published tree.
    candidates = [*_git_tracked_markdown_files(), Path(__file__).resolve()]
    offenders = []
    for path in candidates:
        relative_path = path.relative_to(REPO_ROOT).as_posix()
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "re.compile(" in line:
                # A regex *pattern* line -- this module's own STRAY_COMMAND
                # above included -- legitimately puts a literal separator
                # character next to the word without meaning a path (e.g.
                # STRAY_COMMAND's `\b` escape sitting right after it). That
                # is pattern source, not prose, so it is out of scope here.
                continue
            # Exempt only the URL's own span, not the whole line -- a
            # genuine offender sharing a line with that URL must still be
            # caught, so the exemption cannot be line-wide.
            exempt_spans = [match.span() for match in RASPBERRY_PI_IMAGER_URL.finditer(line)]
            for match in STALE_PATH_REFERENCE.finditer(line):
                if any(start <= match.start() and match.end() <= end for start, end in exempt_spans):
                    continue
                offenders.append(f"{relative_path}:{line_number}: {line.strip()}")
                break  # one entry per offending line is enough to act on

    assert offenders == [], (
        "Found a reference to the pre-migration subdirectory (or a `cd` into it) -- this "
        f"repository's root is already the published tree, so point at it directly: {offenders}"
    )
