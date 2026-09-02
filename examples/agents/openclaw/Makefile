# Bootstraps this kit's local, self-contained state: a `.env` with absolute
# host paths (Docker bind mounts need absolute paths, not the relative
# placeholders in .env.sample) and a freshly generated Gateway token, then
# (via `setup`) the Compose file, a pre-seeded OpenClaw config, and the
# bundled skill. `.env` is a file target, so re-running this after editing
# `.env` by hand is a no-op -- delete it (or run `make clean-state`) to
# regenerate.

# Pinned to the release this kit was verified against, matching the
# OPENCLAW_IMAGE tag in .env.sample and the version recorded in
# THIRD_PARTY_NOTICES.md -- bump all three together after re-verifying the
# walkthrough end-to-end.
DOCKER_COMPOSE_URL := https://raw.githubusercontent.com/openclaw/openclaw/v2026.7.1/docker-compose.yml

.PHONY: setup clean-state

# Order-only prerequisite (`|`): .env.sample must exist, but its timestamp is
# ignored -- once .env exists it is never regenerated, even after a git pull
# updates .env.sample. It holds the user's secrets; regeneration is always the
# explicit `make clean-state` (or deleting .env), never a side effect.
.env: | .env.sample
	cp .env.sample .env
	sed -i.bak \
	  -e 's|^OPENCLAW_CONFIG_DIR=.*|OPENCLAW_CONFIG_DIR=$(CURDIR)/volumes/config|' \
	  -e 's|^OPENCLAW_WORKSPACE_DIR=.*|OPENCLAW_WORKSPACE_DIR=$(CURDIR)/volumes/workspace|' \
	  -e 's|^OPENCLAW_AUTH_PROFILE_SECRET_DIR=.*|OPENCLAW_AUTH_PROFILE_SECRET_DIR=$(CURDIR)/volumes/auth-secrets|' \
	  -e 's|^OPENCLAW_GATEWAY_TOKEN=.*|OPENCLAW_GATEWAY_TOKEN='"$$(openssl rand -hex 24)"'|' \
	  .env
	rm -f .env.bak
	mkdir -p volumes/config volumes/workspace volumes/auth-secrets

# Fetches OpenClaw's published Compose file (no repo clone needed -- the image
# is pulled pre-built from ghcr.io) and pre-seeds config + the bundled skill
# into volumes/, which the Compose file bind-mounts into the container.
# Guards each copy with `test -e` rather than `cp -n`/overwriting so a config
# or skill you've since edited is never clobbered by a re-run.
setup: .env
	test -e docker-compose.yml || curl -sO $(DOCKER_COMPOSE_URL)
	test -e volumes/config/openclaw.json || cp openclaw.json volumes/config/openclaw.json
	test -e volumes/workspace/skills/palmimo || (mkdir -p volumes/workspace/skills && cp -r skills/palmimo volumes/workspace/skills/)

# Wipes all local state (the downloaded Compose file, OpenClaw
# config/workspace/secrets, and the generated .env) so the next `make setup`
# starts from a clean slate. Never touches your personal ~/.openclaw -- that
# directory is never used by this kit.
clean-state:
	rm -rf docker-compose.yml volumes .env
