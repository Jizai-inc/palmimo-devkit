# AGENTS.md — Palmimo DevKit software

> User-facing layer of this uv workspace: the `palmimo_sdk` core
> (motion engine, public API, servo I/O) and the agent apps.

**A procedure has one home** — see [Documentation](#documentation) for which page
that is. This file holds the Python development rules agents need on every task
in this repository.

> **Scope:** the uv / Python rules below apply throughout this repository.

## Quick Reference

- Language: Python >=3.12
- Package manager: **uv** (NEVER pip)
- Build backend: hatchling
- Run from: the repository root
- Execute: `uv run python <script>` (NEVER bare `python`)
- Add deps: `uv add <package>` (NEVER `pip install`)
- Sync deps: `uv sync` (+ `uv sync --group dev` for ruff/mypy)
- Lint/format: `uv run ruff check .` / `uv run ruff format .`
- Type check: `uv run mypy` (bare — pyproject's `files` decides the target set, same as CI; passing `.` would skip it)

## Layout

```
packages/
  palmimo_sdk/                    -> Core SDK — the single user-facing window
    robot.py                   -> Palmimo facade (public API + connection lifecycle)
    engine.py                  -> MotionEngine (pure gait/IK, no I/O)
    kinematics.py              -> Shared leg kinematics
    io/base.py                 -> ServoDriver ABC (I/O boundary)
    io/dynamixel.py            -> DynamixelDriver (concrete, over Dynamixel bus)
    io/camera.py, io/display.py, io/microphone.py, io/speaker.py
                               -> peripheral backends the Palmimo facade owns
    tests/                     -> engine / robot / driver / kinematics tests
examples/
  agents/
    wakeword/                    -> Wake-word voice agent example (Silero VAD + Whisper STT + LLM tool-calling)
    companion/                   -> Always-on companion agent example (idle loop + speech/vision-driven responses, LLM tool-calling)
    openclaw/                    -> Connection kit for driving Palmimo from OpenClaw (self-hosted AI assistant) over the MCP server
scripts/                       -> Supported user diagnostics
tests/                         -> Tests owned by the tree, not by one package
  contracts/                   -> Doc placement, naming, and comment-language ratchets
  scripts/                     -> Tests for scripts/
```

> Anything built on top of the SDK consumes `palmimo_sdk` as its single window.
> The core never depends back on a package that builds on it.

## Architecture (read [doc/explanation/architecture.md](doc/explanation/architecture.md) for details)

The `palmimo_sdk` package is the core; everything else builds on its shared computation:

1. **palmimo_sdk.engine — MotionEngine** — Pure gait/IK computation. NEVER sends hardware commands.
2. **palmimo_sdk.robot — Palmimo** — Public facade. Maps string commands -> Motion enum; owns connection lifecycle.
3. **palmimo_sdk.io — ServoDriver / DynamixelDriver** — I/O boundary; adapts position dicts to the servo bus.

Anything built on the SDK delegates gait to MotionEngine and never opens a
hardware backend of its own.

Data flow: `robot.forward()` -> `robot.step()` -> MotionEngine computes -> returns `Dict[str, int]` (servo_name -> tick_value) -> ServoDriver sends to hardware (or compute-only in dry-run).

## Coding Rules

- SPDX headers (`# SPDX-License-Identifier: Apache-2.0`) apply only to
  `palmimo_sdk` package code (`packages/palmimo_sdk/palmimo_sdk/`, excluding
  its own `tests/`) — never to examples, tests, or scripts. Enforced by
  [tests/contracts/test_license_headers.py](tests/contracts/test_license_headers.py).
- Type hints on ALL function signatures
- Google-style docstrings
- Constants: UPPER_SNAKE_CASE / Classes: PascalCase / Functions: snake_case
- Each gait method only modifies servo positions — no side effects
- New motions MUST transition smoothly from/to neutral stance
- Tests: dry-run first (`--live` flag off), verify servo range 200-3900 (safe range)
- When changing palmimo_sdk (robot.py, engine.py, kinematics.py), update the co-located docs under `doc/` ([api-reference.md](doc/reference/api-reference.md), [architecture.md](doc/explanation/architecture.md), [motion-development-guide.md](doc/guides/motion-development-guide.md)) in the same commit

## Comments

- Everything in this tree ships to users, so the text explaining the code is
  **English only**: Python comments, docstrings, and the text a diagnostic
  carries (an `assert`'s message, a raised exception's arguments, a log
  record), `#` comments in the config files (`pyproject.toml`, YAML, shell,
  `.gitignore`, `.env` samples), Markdown prose, and the labels in
  `doc/images/*.drawio.svg`.
- **The tree carries no translation debt.** `JAPANESE_COMMENT_DEBT`
  ([tests/contracts/test_comment_language.py](tests/contracts/test_comment_language.py))
  is empty, and a test keeps it empty — translate a file rather than parking it
  on the list. Reopening the list for a staged migration means deleting that
  test in the same commit, so the decision is reviewed rather than assumed.
- Document *why*, not *what*. Rationale, alternatives weighed, and change
  history go in the commit message and PR description — not the source.
- The ratchet checks explanation, not output. Japanese in a string the product
  speaks or returns (CLI output including argparse help and usage, LLM-facing
  tool descriptions), in a fenced Markdown sample, or in a runtime text file
  listed in `RUNTIME_TEXT_FILES` (a system prompt an app ships) is outside its
  scope — what language the product itself speaks is a separate decision. Text read while diagnosing the robot is
  explanation, whichever kind of literal it lives in.
- Adding a file format this tree has never carried (a `Dockerfile`, a
  `pytest.ini`, a `.mp4` asset) fails the ratchet until it is classified: give
  the format a check, list a binary suffix, or record why the file carries no
  prose. The rule covers the tree, so nothing enters it unexamined.

## Do NOT

- Use pip or bare python — use uv exclusively
- Hardcode servo IDs — use TRIPOD_A, TRIPOD_B, LEFT_LEGS, RIGHT_LEGS constants
- Send hardware commands from palmimo_sdk.engine (MotionEngine)
- Break the neutral-position-between-motions contract
- Add dependencies without `uv add`
- Skip dry-run testing before live testing

## Adding a New Motion

See [doc/guides/motion-development-guide.md](doc/guides/motion-development-guide.md) for the full checklist.
Minimum: (1) Motion enum, (2) `_apply_*()` method, (3) `step()` dispatch, (4) `_MOTION_MAP` entry,
(5) LLM tool in `palmimo_sdk/agent/` (a `Tool` subclass in `tools.py` plus a `TOOL_MODELS`
entry in `toolset.py`; if a motion is deliberately withheld from the LLM, leave a comment
in `toolset.py` saying so).

## Naming (tests & scripts)

- Put deterministic, automatically judged behavior in pytest, not in an executable check script.
- Put a test next to what it covers: a package's tests in `packages/<package>/tests/`, a rule the
  whole tree must hold in `tests/contracts/`, a test for a supported script in `tests/scripts/`.
- Name pytest files `test_<unit>.py` and tests `test_<subject>_<behavior>[_when_<condition>]`.
- Name executable scripts `<verb>_<object>[_<qualifier>].py`; supported user scripts go in `scripts/`.
- Never use `test_*.py` or `*_test.py` for a manually executed script; those names are reserved for pytest.

## Hardware Safety

- **There is no thermal protection yet.** The driver can now read
  `Present_Temperature` (`ServoDriver.read_telemetry()`), but nothing acts on
  it — no warning, no stop, on an overheating servo. Do NOT assume the robot
  self-protects, and watch temperature by hand during long or stalled runs.
- Always smooth transitions — abrupt jumps damage gears
- `stop()` returns to neutral gradually; NEVER skip it
- Safe servo range: 200-3900 (avoid mechanical limits at 0 and 4095)
- Always test motions in the air before placing the robot on a surface

## Documentation

Every fact has one home; other pages link to it rather than restating it. Where
that home is depends on which tree the page lives in, because the trees answer
to different readers.

| Tree | What it is | Rule |
|---|---|---|
| `README.md` | The product's face | What Palmimo is, plus a Quickstart. Route to the rest; do not host it |
| `doc/guides/` | How-to — the reader has a goal | Steps in order, commands included |
| `doc/reference/` | Reference — the reader wants a fact | Complete and lookup-shaped. No procedures |
| `doc/explanation/` | Explanation — the reader wants to understand | Why it is built this way. No procedures |
| `examples/**/README.md` | Runnable examples | **Self-contained** — an example a reader copies out has to still make sense, so these are not split across `doc/` |
| `docs-site/` | The documentation site | A rendering of `doc/`, built by `sync-doc.mjs`. Write the page in `doc/`; the only page written here is the splash |
| `integrations/**` | Separate uv workspaces | Own their install and run commands; this workspace cannot state them |
| `scripts/README.md` | The user-facing CLI surface | Reference for every subcommand, with its usage |
| `CONTRIBUTING.md` | The contributor loop | Checks, conventions, review |
| `AGENTS.md` | This file | Rules an agent needs on every task |

- **One page, one kind.** A page that answers two of the four is two pages. The
  giveaway is the reader changing state mid-page — finishing a task and starting
  to study, or the audience switching from user to maintainer.
- **Commands go on the page that owns the procedure.** Reference and explanation
  pages, and index pages, link instead. The allowed pages are listed in
  `COMMAND_PAGES` in
  [tests/contracts/test_documentation_contracts.py](tests/contracts/test_documentation_contracts.py),
  and a contract test holds the line.
- **Safety text is the one place duplication is intended.** A warning is worth
  repeating where a reader could act without it.
- **Diagrams are `.drawio.svg` assets in `doc/images/`**, referenced from the
  page. Do not draw box-style ASCII diagrams in Markdown; plain directory trees
  are fine.
- **When you change the SDK core**, update the pages co-located with it in the
  same commit: `doc/reference/api-reference.md`,
  `doc/explanation/architecture.md`, `doc/guides/motion-development-guide.md`.

| Purpose | Reference |
|---------|-----------|
| Install & first run | [doc/guides/installation.md](doc/guides/installation.md) |
| Motion system | [doc/explanation/motion-system.md](doc/explanation/motion-system.md), [doc/reference/motions.md](doc/reference/motions.md) |
| Architecture & SDK design | [doc/explanation/architecture.md](doc/explanation/architecture.md) |
| API reference | [doc/reference/api-reference.md](doc/reference/api-reference.md) |
| New motion checklist | [doc/guides/motion-development-guide.md](doc/guides/motion-development-guide.md) |
