"""Comment-language ratchet for the publicly shipped tree.

Everything in this tree ships to users, so the text a reader of the
source has to understand is English-only: Python comments, docstrings, and
diagnostics, ``#`` comments in the config files, Markdown prose, and diagram
labels.

Japanese the product itself emits stays untouched — the strings it speaks and
returns, fenced code samples, and runtime text files listed in
``RUNTIME_TEXT_FILES`` (a system prompt an app ships) are output, not
explanation.

Files that still carry Japanese are listed in ``JAPANESE_COMMENT_DEBT``. The
list is empty and locked empty by a test: the tree is fully English, so a new
file is translated rather than parked on a list.

Every shipped file must answer to one of those checks, be a binary asset, or
be recorded in ``UNCHECKED_BY_DESIGN`` with the reason it carries no prose —
a format this module has never seen fails rather than passing unexamined.
"""

import ast
import io
import os
import re
import subprocess
import tokenize
from collections.abc import Callable, Mapping
from functools import cache
from pathlib import Path

import pytest


SOFTWARE_ROOT = Path(__file__).resolve().parents[2]
# .cff (Citation File Format) is YAML with a different suffix.
CONFIG_SUFFIXES = frozenset({".toml", ".yaml", ".yml", ".sh", ".json5", ".json", ".cff"})
# CODEOWNERS takes `#` comments and no suffix, like the two below.
CONFIG_NAMES = frozenset({".gitignore", "Makefile", "CODEOWNERS"})
BINARY_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".glb"})
LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})

JAPANESE = re.compile(r"[぀-ゟ゠-ヿ一-鿿、。〃々〆〇〈-』【-〟ｦ-ﾟ]")
HASH_COMMENT_WITH_JAPANESE = re.compile(rf"#[^\n]*{JAPANESE.pattern}")
LINE_COMMENT_WITH_JAPANESE = re.compile(rf"//[^\n]*{JAPANESE.pattern}")
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# A fence indented under a list item is still a code sample, so allow leading space.
# Mirrored as FENCED_CODE in test_documentation_contracts.py (importlib import
# mode leaves sibling test modules unimportable) -- change both together.
FENCED_CODE = re.compile(r"^[ \t]*(```|~~~).*?(^[ \t]*\1|\Z)", re.DOTALL | re.MULTILINE)
INLINE_CODE = re.compile(r"`[^`\n]*`")

# Text the robot speaks, not text explaining the code, so it stays Japanese even
# though everything else in the tree is English.
RUNTIME_TEXT_FILES: frozenset[str] = frozenset(
    {
        # The companion agent's system prompts: character, tool contract, and
        # per-turn-kind (idle/respond) behavior rules the LLM reads at chat
        # time -- output the agent speaks from, not documentation about the
        # agent.
        "examples/agents/companion/palmimo_companion_agent/core/prompts/persona.md",
        "examples/agents/companion/palmimo_companion_agent/core/prompts/identity.md",
        "examples/agents/companion/palmimo_companion_agent/core/prompts/idle.md",
        "examples/agents/companion/palmimo_companion_agent/core/prompts/respond.md",
    }
)

# Shipped files exempt from every check, and why. A reason is required: it is
# what a later reader weighs when deciding whether the exemption still holds.
UNCHECKED_BY_DESIGN: Mapping[str, str] = {
    ".python-version": "a bare interpreter version, with no room for prose",
    **dict.fromkeys(
        (
            "uv.lock",
            # The nested LeRobot workspace resolves its own.
            "integrations/lerobot/uv.lock",
        ),
        "resolved by uv — a failure here could not be fixed by translating",
    ),
    "packages/palmimo_sdk/palmimo_sdk/py.typed": "PEP 561 marker — empty by spec, no room for prose",
    # One copy per distributable unit, so each built wheel ships the license.
    **dict.fromkeys(
        (
            "LICENSE",
            "packages/palmimo_sdk/LICENSE",
            "integrations/lerobot/LICENSE",
            "integrations/lerobot/lerobot_robot_palmimo/LICENSE",
            "integrations/lerobot/lerobot_teleoperator_palmimo/LICENSE",
            "examples/agents/openclaw/LICENSE",
        ),
        "upstream Apache-2.0 text, reproduced verbatim and never edited here",
    ),
}

# Licence text this tree writes rather than reproduces, so it reads as prose
# even where it sits under a file named like a verbatim upstream copy.
AUTHORED_NOTICES: frozenset[str] = frozenset({"docs-site/public/models/LICENSE"})

JAPANESE_COMMENT_DEBT: frozenset[str] = frozenset()


@cache
def _shipped_files() -> tuple[Path, ...]:
    """Return the files that ship: tracked, plus new ones git would accept.

    A developer's own ``.env`` and ``config.yaml`` sit in this tree but are
    git-ignored, so scanning the working tree would hold private notes to the
    published tree's language rule. A file that is merely not staged yet still
    counts — it is on its way in, so a local draft that is not git-ignored is
    in scope. Index entries with no file on disk (a tracked file deleted
    without ``git rm``, a rebase in progress) are dropped.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=SOFTWARE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        unresolved = (
            "git is unavailable, so the shipped file set cannot be resolved and this ratchet "
            "cannot run — a tree unpacked outside git is unprotected"
        )
        if os.environ.get("CI"):
            pytest.fail(unresolved)
        pytest.skip(unresolved)
    return tuple(
        sorted(path for name in listing.stdout.split("\0") if name and (path := SOFTWARE_ROOT / name).is_file())
    )


def _has_japanese_prose(path: Path) -> bool:
    """Report whether a Python source explains itself to a developer in Japanese.

    Comments, docstrings, and the text a diagnostic carries all count.
    Japanese inside any other string literal is treated as user-facing output,
    which this ratchet deliberately ignores.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        # A draft that is not git-ignored is in scope, and the tokenizer accepts
        # sources the parser rejects, so name the file rather than dying inside a
        # helper that never learned which one it was reading.
        pytest.fail(f"{path} does not parse, so it cannot be checked: {error}")
    return _has_japanese_comment_or_docstring(source) or _has_japanese_diagnostic_message(tree)


def _has_japanese_comment_or_docstring(source: str) -> bool:
    """Report whether a Python source carries Japanese in a comment or docstring."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    previous: tokenize.TokenInfo | None = None
    for token in tokens:
        if token.type == tokenize.COMMENT:
            if JAPANESE.search(token.string):
                return True
        elif token.type == tokenize.STRING and JAPANESE.search(token.string):
            # A string opening a logical line is a docstring; anything else is
            # a value the program actually uses.
            starts_statement = previous is None or previous.type in (
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.NEWLINE,
                tokenize.NL,
                tokenize.ENCODING,
            )
            if starts_statement:
                return True
        if token.type not in (tokenize.NL, tokenize.COMMENT):
            previous = token
    return False


def _is_logger(expression: ast.expr) -> bool:
    """Report whether an expression names a logger.

    Held to the receiver's name because the method names alone are too common:
    ``parser.error(...)`` is argparse writing usage text to the operator. What
    language this tree's CLI speaks is unsettled — its examples disagree today
    — so that text is left to whoever settles it rather than decided here.
    """
    if isinstance(expression, ast.Call):
        return _is_logger(expression.func)
    if isinstance(expression, ast.Attribute):
        name = expression.attr
    elif isinstance(expression, ast.Name):
        name = expression.id
    else:
        return False
    return name.lower().strip("_") in {"log", "logger", "logging", "getlogger"}


def _has_japanese_diagnostic_message(tree: ast.Module) -> bool:
    """Report whether a diagnostic explains itself in Japanese.

    An assertion message, a raised exception's arguments, and a log record are
    read by whoever is diagnosing the robot rather than by whoever is talking
    to it, so they are explanation that happens to live in a string literal —
    the one kind the token pass cannot tell apart from the text the product
    emits. Only a literal written into the statement itself is read: a message
    assembled elsewhere and passed in by name is beyond what a ratchet should
    chase.

    The caller parses, so a source that does not compile fails by name.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            messages = [node.msg] if node.msg is not None else []
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            messages = _call_arguments(node.exc)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr not in LOG_METHODS or not _is_logger(node.func.value):
                continue
            messages = _call_arguments(node)
        else:
            continue
        # Walk each message so an f-string or a joined expression is covered too.
        for message in messages:
            for part in ast.walk(message):
                if isinstance(part, ast.Constant) and isinstance(part.value, str) and JAPANESE.search(part.value):
                    return True
    return False


def _call_arguments(call: ast.Call) -> list[ast.expr]:
    """Return every argument of a call, keyword ones included."""
    return [*call.args, *(keyword.value for keyword in call.keywords)]


def _has_japanese_config_comment(path: Path) -> bool:
    """Report whether a config file carries Japanese in a ``#`` comment.

    Configured values may be Japanese — only the explanation around them is
    held to English.
    """
    return HASH_COMMENT_WITH_JAPANESE.search(path.read_text(encoding="utf-8")) is not None


def _has_japanese_js_comment(path: Path) -> bool:
    """Report whether a JS/TS/CSS source carries Japanese in a ``//`` or ``/* */`` comment.

    ``_has_japanese_config_comment`` cannot be reused here: its regex only
    recognizes ``#``, so it would silently never flag a comment in this style.
    """
    text = path.read_text(encoding="utf-8")
    if LINE_COMMENT_WITH_JAPANESE.search(text):
        return True
    return any(JAPANESE.search(block) for block in BLOCK_COMMENT.findall(text))


def _has_japanese_astro_comment(path: Path) -> bool:
    """Report whether an Astro component carries Japanese in a comment.

    A component is two languages in one file, so it is two comment styles: the
    script parts take ``//`` and ``/* */``, the template part takes ``<!-- -->``.
    Checking only the first would leave the template's comments unread.
    """
    text = path.read_text(encoding="utf-8")
    if _has_japanese_js_comment(path):
        return True
    return any(JAPANESE.search(block) for block in HTML_COMMENT.findall(text))


def _has_japanese_markdown_prose(path: Path) -> bool:
    """Report whether a Markdown page carries Japanese outside its code samples."""
    text = path.read_text(encoding="utf-8")
    prose = INLINE_CODE.sub("", FENCED_CODE.sub("", text))
    return JAPANESE.search(prose) is not None


def _has_japanese_label(path: Path) -> bool:
    """Report whether a diagram carries a Japanese label."""
    return JAPANESE.search(path.read_text(encoding="utf-8")) is not None


def _check_for(path: Path) -> Callable[[Path], bool] | None:
    """Return the language check this file's format answers to, if any.

    Format dispatch only. Which files are exempt is policy, decided by the
    caller, so that an unknown format stays distinguishable from an excluded
    one.
    """
    if path.suffix == ".py":
        return _has_japanese_prose
    if path.suffix in {".md", ".mdx"}:
        return _has_japanese_markdown_prose
    if path.suffix == ".astro":
        return _has_japanese_astro_comment
    if path.name == "NOTICE" or path.relative_to(SOFTWARE_ROOT).as_posix() in AUTHORED_NOTICES:
        # Attribution prose we author, unlike the verbatim LICENSE copies.
        return _has_japanese_markdown_prose
    if path.suffix == ".svg":
        return _has_japanese_label
    if path.suffix in {".js", ".mjs", ".ts", ".css"}:
        return _has_japanese_js_comment
    if path.suffix in CONFIG_SUFFIXES or path.name in CONFIG_NAMES or path.name.startswith(".env"):
        return _has_japanese_config_comment
    return None


def _scanners() -> dict[str, Callable[[Path], bool]]:
    """Map every shipped file that carries prose to the check it answers to."""
    scanners = {}
    for path in _shipped_files():
        relative = path.relative_to(SOFTWARE_ROOT).as_posix()
        check = _check_for(path)
        if check is not None and relative not in RUNTIME_TEXT_FILES:
            scanners[relative] = check
    return scanners


def _assert_english(check: Callable[[Path], bool], subject: str, hint: str = "") -> None:
    """Fail with the offending paths when a category still carries Japanese."""
    checked = sorted(relative for relative, scanner in _scanners().items() if scanner is check)
    assert checked, f"No files were scanned for {subject}; the ratchet would pass vacuously."
    offenders = [
        relative for relative in checked if relative not in JAPANESE_COMMENT_DEBT and check(SOFTWARE_ROOT / relative)
    ]
    assert offenders == [], f"{subject} in the shipped tree must be English: {offenders}. {hint}".rstrip()


def test_shipped_python_sources_are_explained_in_english() -> None:
    _assert_english(
        _has_japanese_prose,
        "Comments, docstrings, and diagnostics",
        "Everything shipped in this tree is English-only, test suites included.",
    )


def test_shipped_config_files_have_english_comments() -> None:
    _assert_english(_has_japanese_config_comment, "Config comments")


def test_shipped_js_sources_have_english_comments() -> None:
    _assert_english(_has_japanese_js_comment, "JS/TS comments")


def test_shipped_markdown_has_english_prose() -> None:
    _assert_english(
        _has_japanese_markdown_prose,
        "Markdown prose",
        "Japanese inside fenced samples is sample output and stays as is.",
    )


def test_shipped_diagrams_have_english_labels() -> None:
    _assert_english(_has_japanese_label, "Diagram labels")


def test_every_shipped_file_answers_to_a_decision() -> None:
    # The format dispatch is an allow-list, so a format nobody taught it about
    # would slip through in silence. Force the next one to be classified.
    unclassified = []
    for path in _shipped_files():
        relative = path.relative_to(SOFTWARE_ROOT).as_posix()
        if relative in UNCHECKED_BY_DESIGN or path.suffix in BINARY_SUFFIXES:
            continue
        if _check_for(path) is None:
            unclassified.append(relative)
    assert unclassified == [], (
        f"These shipped files answer to no language check: {sorted(unclassified)}. Give the format a check in "
        "_check_for, record the suffix in BINARY_SUFFIXES, or record the file in UNCHECKED_BY_DESIGN with the "
        "reason it carries no prose. A scratch file belongs outside the tree instead."
    )


def test_exemption_records_name_files_that_still_ship() -> None:
    shipped = {path.relative_to(SOFTWARE_ROOT).as_posix() for path in _shipped_files()}
    stale = sorted(
        f"{relative} ({UNCHECKED_BY_DESIGN.get(relative, 'runtime text')})"
        for relative in set(UNCHECKED_BY_DESIGN) | set(RUNTIME_TEXT_FILES)
        if relative not in shipped
    )
    assert stale == [], (
        f"These exemptions name files that no longer ship: {stale}. Drop the record so it cannot outlive the "
        "file it explains."
    )


def test_the_debt_list_stays_empty() -> None:
    # Reopening the list is a deliberate act: delete this test in the same
    # commit, so staging a migration is reviewed rather than assumed.
    assert not JAPANESE_COMMENT_DEBT, (
        "The tree is fully English. Translate the file instead of reopening the debt list: "
        f"{sorted(JAPANESE_COMMENT_DEBT)}"
    )


def test_debt_list_drops_files_once_they_are_translated() -> None:
    scanners = _scanners()
    resolved = sorted(
        entry for entry in JAPANESE_COMMENT_DEBT if entry not in scanners or not scanners[entry](SOFTWARE_ROOT / entry)
    )
    assert resolved == [], (
        "These files no longer carry Japanese (or no longer exist). "
        f"Remove them from JAPANESE_COMMENT_DEBT so the ratchet holds: {resolved}"
    )


def test_japanese_module_docstring_after_header_comment_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text('# header comment\n"""日本語の docstring"""\n', encoding="utf-8")
    assert _has_japanese_prose(source)


def test_halfwidth_katakana_comment_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("# ﾃｽﾄ\n", encoding="utf-8")
    assert _has_japanese_prose(source)


def test_japanese_assertion_message_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text('assert value is not None, "value が None"\n', encoding="utf-8")
    assert _has_japanese_prose(source)


def test_japanese_raised_error_message_is_detected(tmp_path: Path) -> None:
    # Setup guidance an operator reads on the CLI is raised the same way a
    # programmer-only contract violation is, and this tree publishes both.
    source = tmp_path / "module.py"
    source.write_text('raise RuntimeError("ASR model が未設定です")\n', encoding="utf-8")
    assert _has_japanese_prose(source)

    source.write_text('raise ValueError(message=f"{value} は未設定")\n', encoding="utf-8")
    assert _has_japanese_prose(source)


def test_japanese_log_record_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text('self._log.warning("録音に失敗: %s", exc)\n', encoding="utf-8")
    assert _has_japanese_prose(source)

    source.write_text('logging.getLogger(__name__).exception("検知器が失敗")\n', encoding="utf-8")
    assert _has_japanese_prose(source)


def test_a_source_that_does_not_parse_fails_by_name(tmp_path: Path) -> None:
    # The tokenizer accepts what the parser rejects, so a draft in the tree can
    # reach the ast pass broken. The failure has to say which file it was.
    source = tmp_path / "broken.py"
    source.write_text("x = = 1\n", encoding="utf-8")
    with pytest.raises(pytest.fail.Exception, match=r"broken\.py"):
        _has_japanese_prose(source)


def test_japanese_argparse_usage_text_is_ignored(tmp_path: Path) -> None:
    # Held to the receiver's name: argparse writes usage text to the operator,
    # alongside the --help strings whose language this ratchet does not decide.
    source = tmp_path / "module.py"
    source.write_text('parser.error("--no-stdin は --headless と併用してください")\n', encoding="utf-8")
    assert not _has_japanese_prose(source)


def test_japanese_the_assertion_compares_against_is_ignored(tmp_path: Path) -> None:
    # The compared value is what the product says; only the message explains.
    source = tmp_path / "module.py"
    source.write_text('assert spoken == "踊って", "the dance command is not recognized"\n', encoding="utf-8")
    assert not _has_japanese_prose(source)


def test_japanese_the_product_returns_is_ignored(tmp_path: Path) -> None:
    # The same function may speak Japanese and explain itself in English.
    source = tmp_path / "module.py"
    source.write_text('def speak() -> str:\n    return "こんにちは"\n', encoding="utf-8")
    assert not _has_japanese_prose(source)


def test_trailing_config_comment_is_detected_while_the_value_is_ignored(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text('greeting: "こんにちは"  # 挨拶\n', encoding="utf-8")
    assert _has_japanese_config_comment(config)

    config.write_text('greeting: "こんにちは"  # spoken on wake\n', encoding="utf-8")
    assert not _has_japanese_config_comment(config)


def test_a_fence_indented_under_a_list_item_is_still_a_code_sample(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text("- Step one:\n\n  ```text\n  発話: 踊って\n  ```\n", encoding="utf-8")
    assert not _has_japanese_markdown_prose(page)


def test_markdown_code_samples_may_stay_japanese_while_prose_may_not(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text('```python\nrobot.say("こんにちは")\n```\n\nCall `robot.say("やあ")` to speak.\n', encoding="utf-8")
    assert not _has_japanese_markdown_prose(page)

    page.write_text("The robot speaks 日本語 here.\n", encoding="utf-8")
    assert _has_japanese_markdown_prose(page)
