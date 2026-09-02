# Third-Party Notices

This directory has no `pyproject.toml` and declares no Python or Node
dependencies of its own — see [README.md](README.md) for why. The one
third-party component it reaches for is OpenClaw itself, resolved at run
time rather than vendored: `make setup` fetches OpenClaw's published
`docker-compose.yml`, which Compose resolves against `OPENCLAW_IMAGE` from
[`.env.sample`](.env.sample) — both pinned to the same OpenClaw release
(see the [README's Notes](README.md#notes)).

### OpenClaw

- License: MIT
- Copyright: OpenClaw Foundation
- Source: https://github.com/openclaw/openclaw
- Resolved as: `ghcr.io/openclaw/openclaw:2026.7.1`, the image
  [`.env.sample`](.env.sample) names for the tag-pinned Compose file this kit
  fetches. Nothing from OpenClaw is vendored in this directory.
