# Examples

Demo and sample apps built on top of `palmimo_sdk`. Each consumes the SDK as
its single window into the hardware — none of them open a servo bus or motion
backend directly. The Python ones (`teleop/`, `agents/wakeword/`,
`agents/companion/`) are each a standalone uv project — their own
`pyproject.toml` and `uv.lock` — depending on the released `palmimo-sdk` from
PyPI, not on this repository's SDK source, so a copy of one works the same
outside this repository. `agents/openclaw/` carries no Python at all.

To try an example against local, unreleased SDK changes
instead, run it with an editable overlay from inside the example's own
directory, through `python -m` rather than a console script (a script in the
example's venv runs that venv's interpreter directly and never sees the
overlay), e.g. `uv run --with-editable ../../packages/palmimo_sdk python -m
palmimo_teleop.main` or `... python -m pytest` (the relative path to
`packages/palmimo_sdk` depends on how deep the example sits under `examples/`
— two levels for `teleop/`, three for `agents/*`).

- `agents/` — Standalone agent implementation examples, one uv project each;
  see [agents/README.md](agents/README.md) for what each one is.
- `teleop/` — Web-based manual teleoperation: a phone/laptop browser drives
  the robot over a WebSocket and views its head camera over MJPEG, on the
  same LAN with no authentication. See
  [teleop/README.md](teleop/README.md).

## Running

Run each example from inside its own directory, not from the repository root:
`cd` into it, `uv sync`, then `uv run <console-script>`. Each agent's own
launch command, CLI options, and design notes live in its own README --
[agents/wakeword/README.md](agents/wakeword/README.md),
[agents/companion/README.md](agents/companion/README.md), and
[agents/openclaw/README.md](agents/openclaw/README.md), which is a connection
kit rather than a uv project. `teleop/`'s own launch command and options are
in [teleop/README.md](teleop/README.md).
