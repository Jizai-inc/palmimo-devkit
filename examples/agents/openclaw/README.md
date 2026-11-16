# OpenClaw connection kit

A connection kit for driving Palmimo from [OpenClaw](https://docs.openclaw.ai)
-- a self-hosted, Docker-run AI assistant, MIT-licensed by the OpenClaw
Foundation (see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)). There is no
Python code here: this is a set of config templates, a `Makefile` that
isolates OpenClaw's local state, and a skill, wiring OpenClaw's Docker
deployment to the [palmimo_sdk MCP server](../../../doc/guides/mcp-server.md)
over streamable HTTP.

Unlike `wakeword/` and `companion/`, this directory is not a member of this
repository's uv workspace -- OpenClaw itself is a separate, Node-based project. It
needs no clone: `make setup` (below) fetches OpenClaw's Compose file alone, and
this kit's `.env` points that file at a published image
(`ghcr.io/openclaw/openclaw`). This kit only prepares the environment, config, and skill it needs to
find Palmimo.

## Prerequisites

- Docker Compose v2 on the machine you'll run OpenClaw on (your PC, not the
  robot).
- `make`, plus the POSIX tools its recipes call (`sh`, `cp`, `sed`, `rm`,
  `mkdir`, `curl`, `openssl`) — every step below goes through this
  directory's `Makefile`. macOS and Linux have these already. On Windows, run
  the steps from Git Bash together with a `make` binary such as the
  [ezwinports](https://sourceforge.net/projects/ezwinports/) build, which
  covers the whole walkthrough.
- `palmimo_sdk` set up on the robot with the `mcp` extra installed, per the
  [MCP server guide](../../../doc/guides/mcp-server.md).
- An API key for whichever LLM provider you'll have OpenClaw chat with (only
  needed once you want it to actually run tool calls, not just discover
  them -- see Step 6).

## Step 1 -- start the MCP server on the robot

On the robot, start `palmimo_sdk.mcp` with the `http` transport, bound to a
host your PC can reach (the endpoint path is always `/mcp`), and a token so
it isn't wide open -- binding a non-loopback host without one is only safe on
a network you trust. The launch command, the transport/token flags, and the
`--no-servo` / `--no-display` / `--no-speaker` / `--no-camera` / `--include`
/ `--exclude` flags to narrow the exposed peripherals or tools are documented
in the [MCP server guide](../../../doc/guides/mcp-server.md) -- this
kit doesn't restate them.

## Step 2 -- generate this kit's `.env`

From this directory:

```bash
make .env
```

This copies [`.env.sample`](.env.sample) to `.env`, rewrites its three
`OPENCLAW_*_DIR` variables to absolute paths under `./volumes/` (Docker bind
mounts need absolute host paths), and generates a random
`OPENCLAW_GATEWAY_TOKEN`. Edit the generated `.env` and fill in:

- `PALMIMO_MCP_URL` -- `http://<robot-host>:8765/mcp`, pointing at the
  server from Step 1.
- `PALMIMO_MCP_TOKEN` -- the same secret the server was started with.

## Step 3 -- fetch Compose and pre-seed config

```bash
make setup
```

Fetches OpenClaw's published `docker-compose.yml` into this directory (no
OpenClaw clone -- the file resolves its image from `OPENCLAW_IMAGE` in `.env`,
which this kit points at a published image), then, if not already present, copies [`openclaw.json`](openclaw.json)
to `volumes/config/openclaw.json` and [`skills/palmimo`](skills/palmimo) to
`volumes/workspace/skills/palmimo`.

The pre-seeded `openclaw.json` sets `gateway.mode: "local"` and registers the
`palmimo` MCP server -- this is what OpenClaw's own interactive
`openclaw setup` onboarding would otherwise ask for, skipped here because this
kit only runs the Gateway container, never the onboarding wizard. Without a
config at all, the Gateway crash-loops on "Missing config." `make setup`
never overwrites a config or skill copy you've already edited; delete the
specific file (or run `make clean-state`) to reseed it.

OpenClaw itself reads the file as JSON5 (comments and unquoted keys are
fine), but the template sticks to strict JSON so editors that lint `.json`
files as plain JSON don't flood it with false errors.

## Step 4 -- bring it up

```bash
docker compose up -d openclaw-gateway
```

Compose's `env_file: .env` imports this kit's `.env` into the container
wholesale, so `PALMIMO_MCP_URL` / `PALMIMO_MCP_TOKEN` reach the process that
expands `${PALMIMO_MCP_URL}` / `${PALMIMO_MCP_TOKEN}` in
`volumes/config/openclaw.json` -- no extra wiring needed. Give it a few
seconds to report healthy.

## Step 5 -- verify the MCP connection

```bash
docker compose run --rm openclaw-cli mcp probe palmimo
```

A working connection reports `palmimo: 24 tools` (the full Palmimo agent
toolset, discovered over streamable HTTP -- the count is however many tools
`AgentToolSet` exposes, so it moves whenever the toolset gains one).
`docker compose run --rm openclaw-cli mcp status` shows the same server
listed with its transport.
Both work without an LLM API key -- probing and discovering tools doesn't
need one.

## Step 6 -- chat with it

To have OpenClaw actually run those tools (not just discover them), add your
LLM provider's API key to `.env` (see the commented example in
[`.env.sample`](.env.sample)) and configure the matching provider/model --
covered by OpenClaw's own docs, out of scope here. Then open the dashboard at
`http://localhost:18789` and try something like "wave and say hello".

A hosted provider's API key is all this kit needs: the runtime the pinned image
already ships with drives the Palmimo tools as-is, so no change to
`openclaw.json` beyond the provider/model settings is required. Pointing
OpenClaw at a locally hosted model instead needs additional configuration of
its own, which this kit does not carry and which has not been tried here —
follow OpenClaw's docs for that path.

The first visit from each browser asks for one-time device pairing and shows a
request id; approve it from this directory with

```bash
docker compose run --rm openclaw-cli devices approve <request-id>
```

then reconnect in the browser.

## Try it without the robot

To sanity-check the OpenClaw side alone, run `palmimo_sdk.mcp` on the same PC
instead of the robot, with every peripheral disabled (`--no-servo
--no-display --no-speaker --no-camera` -- see the
[MCP server guide](../../../doc/guides/mcp-server.md) for the
full command), and point `PALMIMO_MCP_URL` at
`http://host.docker.internal:8765/mcp` in this kit's `.env` (the container
reaches the host through that address; the Compose file wires up
`host.docker.internal` for you). Tool calls will compute and return without
moving anything, which is enough to confirm the MCP connection and skill are
working end to end before touching real hardware.

## State & teardown

Every path this kit touches -- `OPENCLAW_CONFIG_DIR`, `OPENCLAW_WORKSPACE_DIR`,
`OPENCLAW_AUTH_PROFILE_SECRET_DIR`, and the downloaded `docker-compose.yml` --
lives under this directory, generated by `make .env` / `make setup` -- your
personal `~/.openclaw` is never read or written by this kit. `volumes/` holds
OpenClaw's config, workspace, and auth secrets (including whatever LLM API
key you give it), so it, the generated `.env`, and `docker-compose.yml` are
all git-ignored.

```bash
make clean-state
```

removes `docker-compose.yml`, `volumes/`, and `.env` entirely, so the next
`make .env && make setup` starts clean.

## Notes

- The fetched Compose file and `OPENCLAW_IMAGE` are pinned to the OpenClaw
  release this kit was verified against, so the walkthrough keeps reproducing
  a known-good combination rather than tracking OpenClaw's latest. To upgrade,
  bump the tag in the `Makefile`, in `.env.sample`, and in
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) together, then re-run the
  walkthrough. This kit automates OpenClaw's documented Docker path minus
  the interactive onboarding wizard; if you'd rather follow the interactive
  official flow (cloning OpenClaw and running its `scripts/docker/setup.sh`),
  see [OpenClaw's Docker docs](https://docs.openclaw.ai/install/docker) --
  just keep the `OPENCLAW_*_DIR` variables pointing at this kit's `volumes/`
  so state stays isolated.
- The MCP server is **not preemptive**: a running motion completes before the
  next tool call starts, and calls are serialized -- `stop` cannot interrupt
  one already in flight. The skill documents the practical implications; see
  the [MCP server guide](../../../doc/guides/mcp-server.md)
  for the server-side flags (`--include` / `--exclude`, peripheral toggles)
  that shape which tools are exposed in the first place.
- Expect the robot to start moving some seconds after you give an instruction,
  not instantly. In a single measurement here, with a hosted OpenAI key, it was
  roughly 23.7 s from instruction to the robot starting to move — about 18.3 s
  of that in the LLM round trip and about 5.4 s in `docker compose run`'s
  container start. One run on one setup, so treat it as an order of magnitude:
  your model, network, and host all move it.
- OpenClaw's own Gateway listens on port 18789 (plus 18790 and 3978) by
  default -- if you already run another OpenClaw instance on the same
  machine, adjust the port mappings in `.env` before bringing this one up
  (see OpenClaw's own Docker Compose docs).
- The Compose file's bind mounts run as whatever uid the container's `node`
  user is (uid 1000 upstream); if a Linux host denies the container write
  access to `volumes/`, `chown -R 1000:1000 volumes/` fixes it (not needed on
  macOS Docker Desktop).
