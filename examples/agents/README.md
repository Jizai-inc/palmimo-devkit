# Agents

Independent agent implementation examples. `wakeword/` and `companion/` are
each their own standalone uv project (own `pyproject.toml`/`uv.lock`), not
members of the repository's root workspace. Standalone, deliberately simple
agent designs that do not share code with each other.

- `wakeword/` — Minimal wake-word agent: Silero VAD segmentation, OpenAI
  Whisper transcription (wake-word detection and command text alike), and
  single-round OpenAI tool-calling against the Palmimo SDK.
- `companion/` — Always-on companion agent: a continuous LiteLLM
  tool-calling ReAct loop, guarded speech routing (noise / command /
  question / ambient classification before anything reaches the dialogue
  loop), and non-LLM vision reflexes (wave-back, face tracking).
- `openclaw/` — Connection kit (no Python code, not a uv project) wiring
  [OpenClaw](https://docs.openclaw.ai), a self-hosted Docker-run assistant, to
  the `palmimo_sdk` MCP server over streamable HTTP: config templates, a
  `Makefile` that isolates its local state, and a skill.

## Running

Each agent installs and runs from inside its own directory, not from the
repository root. Its launch command, CLI options, and design notes live in
its own README -- [wakeword/README.md](wakeword/README.md),
[companion/README.md](companion/README.md), and
[openclaw/README.md](openclaw/README.md) (which runs from its own directory
too, but is not a uv project).
