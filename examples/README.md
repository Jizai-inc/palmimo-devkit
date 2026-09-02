# Examples

Demo and sample apps built on top of `palmimo_sdk`. Each consumes the SDK as
its single window into the hardware — none of them open a servo bus or motion
backend directly. The Python ones are uv projects in this workspace
(`packages/*` + `examples/*` + `examples/agents/*` in the root
`pyproject.toml`); `agents/openclaw/` carries no Python and is deliberately not
a member.

- `agents/` — Standalone agent implementation examples, one uv project each;
  see [agents/README.md](agents/README.md) for what each one is.

## Running

Run everything from the repository root, not from inside an individual example
directory. Install the workspace first — see the
[installation guide](../doc/guides/installation.md). Each agent's own launch
command, CLI options, and design notes live in its own README --
[agents/wakeword/README.md](agents/wakeword/README.md),
[agents/companion/README.md](agents/companion/README.md), and
[agents/openclaw/README.md](agents/openclaw/README.md), whose kit runs from its
own directory rather than the repository root.
