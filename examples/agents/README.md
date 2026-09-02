# Agents

Independent agent implementation examples, each its own uv project in this
workspace. Standalone, deliberately simple agent designs that do not share
code with each other.

- `wakeword/` — Minimal wake-word agent: Silero VAD segmentation, OpenAI
  Whisper transcription (wake-word detection and command text alike), and
  single-round OpenAI tool-calling against the Palmimo SDK.
- `companion/` — Always-on companion agent: a continuous LiteLLM
  tool-calling ReAct loop, guarded speech routing (noise / command /
  question / ambient classification before anything reaches the dialogue
  loop), and non-LLM vision reflexes (wave-back, face tracking).
- `openclaw/` — Connection kit (no Python code, not a member of this
  repository's uv workspace) wiring [OpenClaw](https://docs.openclaw.ai), a
  self-hosted Docker-run assistant, to the `palmimo_sdk` MCP server over
  streamable HTTP: config templates, a `Makefile` that isolates its local
  state, and a skill.

## Running

Run everything from the repository root, not from inside an individual
example directory. Install the workspace first — see the
[installation guide](../../doc/guides/installation.md). Each agent's own launch
command, CLI options, and design notes live in its own README --
[wakeword/README.md](wakeword/README.md),
[companion/README.md](companion/README.md), and
[openclaw/README.md](openclaw/README.md) (the last one runs from its own
directory, since it isn't a workspace member).
