# MCP Server

`palmimo_sdk.mcp` exposes the same agent tools the wake-word example drives
(forward, wave, set_face, say, capture, ...) over the
[Model Context Protocol](https://modelcontextprotocol.io/), so any
MCP-speaking client — Claude Code, Claude Desktop, or another MCP agent — can
list and call them directly. Built on the MCP Python SDK 2.x, which can
negotiate both the latest protocol revision (2026-07-28) and older revisions
(2024-11-05 through 2025-11-25) with whatever client connects.

## Installing

```bash
# From this workspace root
uv sync --extra mcp

# Or, when using `palmimo_sdk` standalone, outside this workspace
uv add "palmimo-sdk[mcp]"
```

From this workspace root you rarely need either line: `uv sync --dev` (what CI
runs, and what `uv sync` does by default) already pulls `mcp` in through the
`dev` dependency group, and `vision` (head camera, `capture`) and `voice` (the
microphone-side stack) arrive with the example agents. The `--extra` flags
matter when using `palmimo_sdk` standalone, outside this workspace, where none
of that applies.

The servo bus (`dynamixel-sdk`), face display, and speaker dependencies
already install with this workspace's defaults, so no additional extra is
needed for motions, expressions, or speech here; standalone `palmimo-sdk`
installs outside this workspace add `[hardware]`, `[face]`, and `[speech]`
for those.

## Running

Over stdio, for a client on the same machine as the robot:

```bash
uv run python -m palmimo_sdk.mcp
```

Unset `PALMIMO_MCP_TOKEN` first (or prefix the command with `env -u
PALMIMO_MCP_TOKEN`) — stdio cannot check a token, so leaving one set makes
this command fail on purpose rather than serve unauthenticated. See
[Authentication](#authentication) for why.

Or over the network (streamable HTTP, for a client on a different machine
than the robot — e.g. developing from a PC against a Palmimo running on a Pi):

```bash
uv run python -m palmimo_sdk.mcp --transport http --host 0.0.0.0 --port 8765
```

⚠️ Without a token this server has **no authentication** — only bind a
non-loopback `--host` (`0.0.0.0` or similar) on a network you trust. Set
`PALMIMO_MCP_TOKEN=<secret>` (http transport only) to require every request to
carry a matching `Authorization: Bearer <secret>` header:

```bash
PALMIMO_MCP_TOKEN=<secret> uv run python -m palmimo_sdk.mcp --transport http --host 0.0.0.0 --port 8765
```

## Registering With Claude Code

```bash
# Local (client runs on the same machine as the robot, stdio transport). The
# client's cwd is not necessarily this repository's root, so point `uv run`
# at it explicitly with `--directory` (the trailing `palmimo-devkit` is the
# directory name `git clone` creates by default; adjust it if you renamed
# that directory):
claude mcp add palmimo -- uv run --directory <path-to>/palmimo-devkit python -m palmimo_sdk.mcp

# Remote (client is elsewhere; the robot runs the http transport command above)
claude mcp add --transport http palmimo http://<robot-host>:8765/mcp

# Remote, with a token configured: pass the same secret back as a Bearer header.
# The name and URL come FIRST here: `--header` is variadic, so leading with it
# swallows every following word -- the server name and the URL included, which
# is why the command then fails with `missing required argument 'name'`.
claude mcp add palmimo http://<robot-host>:8765/mcp --transport http --header "Authorization: Bearer <secret>"
```

## CLI Flags

| Flag | Default | Meaning |
|---|---|---|
| `--transport {stdio,http}` | `stdio` | MCP transport. |
| `--host` | `127.0.0.1` | Bind address for `--transport http`. |
| `--port` | `8765` | Bind port for `--transport http`. |
| `--token` | `$PALMIMO_MCP_TOKEN` | Require `Authorization: Bearer <token>` on every request (`--transport http` only). Prefer the environment variable over the flag -- a command-line value is visible to other local users via `ps`. |
| `--servo-port` | auto-detect | Explicit servo bus serial port. |
| `--no-servo` / `--no-display` / `--no-speaker` / `--no-camera` | off | Skip attaching (and therefore probing) the matching peripheral -- useful when running on a development PC where probing would otherwise grab a webcam or the default audio device. |
| `--speaker-device` | `ReSpeaker` | ALSA playback card `say` speaks through, matched by a substring of the card id or its long name rather than by an index a USB replug can change. Pass an empty string for whatever ALSA considers default. |
| `--include` | (all tools) | Comma-separated tool names to expose exclusively. |
| `--exclude` | (none) | Comma-separated tool names to hide. |
| `--log-tool-calls {off,summary,full}` | `$PALMIMO_MCP_LOG_TOOL_CALLS`, else `summary` | How much of each tool call to log to stderr -- see [Tool-Call Logging](#tool-call-logging). |

## Authentication

The two transports handle a token differently:

- **stdio** (a client on the same machine as the robot) has no request
  boundary a bearer check can hook into, so it cannot authenticate a client
  at all. If `PALMIMO_MCP_TOKEN` happens to be set in the shell that starts
  it, the command refuses to start with a usage error (exit 2) instead of
  quietly serving unauthenticated from a shell that looks token-protected.
  Unset the variable, or prefix the command with `env -u PALMIMO_MCP_TOKEN`.
- **streamable HTTP** (a client on a different machine than the robot) can
  require a token: set `PALMIMO_MCP_TOKEN=<secret>` and every request must
  carry a matching `Authorization: Bearer <secret>` header. Without a token,
  a server bound to a non-loopback host has no authentication at all — only
  bind `0.0.0.0` (or similar) on a network you trust.

`--token <secret>` works the same as the environment variable and takes
precedence when both are given, but prefer the environment variable: a
command-line value shows up in `ps` output (and similar process listings) to
any other local user on the same machine.

## Degraded Behavior With Missing Peripherals

A peripheral that was unreachable when the server started is not hidden from
the tool list: its tools stay listed and answer a call with a descriptive
not-attached message, rather than disappearing or the server refusing to
start. The server states this in the `instructions` it returns from
`initialize`, so a client sees it up front.

## Tool-Call Logging

Every tool call is logged to **stderr**, one `INFO` line per call — the tool
name, its argument names, the outcome (`ok`, `error`, or `interrupted`), how
long the call took, and the size of the result:

```
2026-08-09 12:00:00,000 INFO palmimo_sdk.mcp.server tool call name=forward args={seconds=1.5} outcome=ok duration_ms=1512.4 result=<str, 22 chars> images=0
```

Nothing is written to stdout, which on the stdio transport carries the MCP
protocol itself. `--log-tool-calls` (or `PALMIMO_MCP_LOG_TOOL_CALLS`) sets how
much of a call the line carries:

| Value | What the line carries |
|---|---|
| `summary` (default) | Argument names, plus the values of numeric arguments. String values are reduced to their length, so caller-supplied text — `say`'s spoken text, and anything a future tool takes — is not copied into the log. |
| `full` | Every argument value and the result text verbatim. For debugging a specific call. |
| `off` | No per-call line. |

```bash
PALMIMO_MCP_LOG_TOOL_CALLS=full uv run python -m palmimo_sdk.mcp
```

## Related Documents

- [`build_mcp_server()`](../reference/api-reference.md#palmimo_sdkmcp) — the API behind this CLI
- [OpenClaw connection kit](../../examples/agents/openclaw/README.md) — driving this server from a container
