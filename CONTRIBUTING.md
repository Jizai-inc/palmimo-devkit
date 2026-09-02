# Contributing to Palmimo DevKit

Thanks for your interest in Palmimo! Bug reports, fixes, docs, and new motions
are all welcome. This guide covers how to get set up, how we review, and how
changes get merged.

- Architecture and coding rules: [AGENTS.md](AGENTS.md)
- Setup and first run: [README.md](README.md) — Quickstart, plus links to
  where each command's procedure actually lives (e.g.
  [doc/guides/installation.md](doc/guides/installation.md) for install)
- Cutting a release: [doc/guides/releasing.md](doc/guides/releasing.md)

## Ways to contribute

- **Bug reports and feature requests** — open an issue and describe what you
  expected, what happened, and how to reproduce it.
- **Code and docs** — open a pull request (see below).
- **Questions and ideas** — see [SUPPORT.md](SUPPORT.md) for where each kind
  of conversation belongs.

## Scope: what lives here

This repository is Palmimo's software stack — the `palmimo_sdk` core, the
examples, the supported scripts, and their documentation — and it is where
pull requests are accepted. The hardware design (electronics, CAD, assembly),
the display firmware, the maintainers' tuning benches, and the simulation
stack live in a private internal repository today. The simulation robot model
and learning environments are planned to open progressively (see
[What's open](README.md#-whats-open-and-what-ships)); the hardware design is
not published. For changes on that side, open an issue describing what you
need and the maintainers implement it internally.

## Language

Issue and pull request conversations are welcome in English or Japanese.
Please keep the recorded outcome in English — the pull request description
and the concluding summary of a discussion — since the tree itself (code,
comments, docs) is English only.

## License of contributions

Palmimo DevKit is released under the [Apache License 2.0](LICENSE). By opening a
pull request you agree that your contribution is licensed under those same terms
— GitHub's default of "inbound = outbound." **There is no CLA and no sign-off
(DCO) to complete**; just open the pull request.

Only submit work you have the right to license this way: your own code, or
code under an Apache-2.0-compatible license. If you adapted something, say
where it came from in the pull request so the attribution can be kept.

By participating you also agree to uphold our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

All Python work happens at the repository root — a **uv** workspace, so
use `uv`, never `pip` or a bare `python`. The SDK core lives in
`packages/palmimo_sdk`; the demo apps and the agent runtime live under
`examples/`. Follow the [installation guide](doc/guides/installation.md) for the
exact install commands, then add the dev dependencies:

```bash
uv sync --group dev
```

## Running the checks

Automated regression tests live under each package's `tests/` directory. Tests
the tree owns rather than a package sit in `tests/`: the convention,
documentation, and comment-language ratchets in `contracts/`, and the tests for
`scripts/` in `scripts/`.

```bash
uv run pytest                                  # whole workspace
uv run pytest packages/palmimo_sdk/tests       # SDK only
uv run pytest --cov                            # with a coverage report
```

CI holds the whole-workspace run to a coverage floor, so a change that adds
code without tests can pass locally and fail there. `--cov` locally reports the
same number; what it measures is `[tool.coverage.run]` in `pyproject.toml`, and
the floor itself is in `.github/workflows/ci.yml`. Deliberately not in
`addopts`: a run of one directory's tests covers a fraction of the tree by
definition, and a floor here would fail that run for being what it is.

Adding a motion? [Step 6 of the motion development
guide](doc/guides/motion-development-guide.md#step-6-test) says what its tests
have to cover — safe tick range, smooth return to neutral, stable tail, both
left/right variants, and boundary values for every public tuning knob.

**ruff** checks PEP-8 compliance, naming conventions, import ordering, and
potential bugs; configuration lives in `pyproject.toml`'s `[tool.ruff]`
section. **mypy** performs static type checking; external-library errors are
suppressed (`ignore_missing_imports = true`), so anything it reports is in
code you wrote — configuration lives in `pyproject.toml`'s `[tool.mypy]`
section.

```bash
uv run ruff check .                            # lint
uv run ruff check . --fix                      # auto-fix what can be auto-fixed
uv run ruff format .                           # format
uv run mypy                                    # same target set as CI, from pyproject
uv run mypy packages/palmimo_sdk/              # one directory only
```

## Before you open a pull request

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv lock --check
```

**All of them are required:** CI runs on Python 3.12 (the version the whole
workspace targets) and blocks on lint, format, type, test, coverage, and
lockfile failures. If you add a dependency, commit the updated `uv.lock`
alongside it.

Two CI jobs have no local equivalent above. One builds the `palmimo-sdk` wheel,
installs it into a venv holding nothing else, and imports it: the checks above
all run in the dev environment, where every optional dependency is present, so
only that job can catch a top-level `import` of one. It runs on 3.13 as well as
3.12, which is what holds the SDK to the `requires-python = ">=3.12"` it
publishes -- the workspace itself cannot be installed on 3.13 today, because the
companion example pins mediapipe 0.10.18 and it ships cp312 wheels only. The
other builds the documentation site, which is the one part of this tree that is
not Python.

When you change the SDK core (`palmimo_sdk`), update the co-located docs under
[doc/](doc/) — `reference/api-reference.md`, `explanation/architecture.md`, and
`guides/motion-development-guide.md` — in the **same commit** as the code.
[AGENTS.md](AGENTS.md#documentation) says which of those a given page is, and
where a new page belongs.

## Pull request conventions

- **Explain _why_, not just _what_.** The description should say what problem
  the change solves and how you verified it. Pull requests that only restate the
  diff will be asked for that context before review.
- **[Conventional Commits](https://www.conventionalcommits.org/)** for titles
  (`feat(scope):`, `fix(scope):`, `docs(scope):`, `chore(scope):`) — the title
  becomes the squash-merge commit message.
- **One home per procedure** — put a runnable example on the page the reader is
  already standing on when they need it, and link to it from everywhere else.
  See [AGENTS.md](AGENTS.md#documentation); the pages allowed to carry commands
  are listed in `COMMAND_PAGES` and held by a contract test.
- Keep each pull request focused; split unrelated changes into separate ones.

## How we review and merge

- A maintainer triages new issues and pull requests. If this is your first
  contribution, GitHub asks a maintainer to approve the CI run before it
  starts — that approval is routine, not a judgement on the change.
- Review needs CI green as its starting point. A maintainer reviews the
  change; if it affects how the robot moves or draws power, it also gets
  on-robot verification (next section) before merge.
- Merges are squash merges performed by a maintainer, so the pull request
  title becomes the commit message and the commit keeps you as the author.
  Nobody pushes directly to `main`.

## Hardware-affecting changes

If a change alters how the robot moves or draws power — gaits, kinematics,
servo I/O, motion timing, servo limits — say so in the pull request; the
template has a checkbox for it. **The robot has no thermal protection yet**,
so a maintainer verifies such changes on hardware before merge, watching
range of motion, current draw, and servo temperature. You do not need a
robot to contribute: the dry-run test suite covers the computation, and a
maintainer runs the on-robot pass for you. That pass depends on a robot and
a maintainer being available, so expect a hardware-affecting pull request to
take noticeably longer to merge than a software-only one.

If you do run your own robot, follow the safety rules in
[AGENTS.md](AGENTS.md): test new motions in the air before floor runs, keep
servo positions inside the safe tick range, and watch servo temperature by
hand during long or stalled runs.

## Coding conventions

See [AGENTS.md](AGENTS.md) for the full rules. In short:

- Python ≥ 3.12, type hints on every signature, Google-style docstrings.
- `uv` only — never `pip` or a bare `python`.
- Everything explaining the code is English only: comments, docstrings, and the
  text a diagnostic carries — an `assert` message, a raised exception's
  arguments, a log record. What the robot says to its user may stay Japanese.
- Deterministic, automatically judged behavior belongs in pytest, not in an
  ad-hoc check script.

## Reporting security issues

Please don't open a public issue for a security or safety problem — see
[SECURITY.md](SECURITY.md) for private reporting.
