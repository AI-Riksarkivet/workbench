.PHONY: help install sync local test lint fmt typecheck check frontend-check frontend-fmt app-dev app-e2e image claude-bootstrap openfga fga-test fga-model

# Every image and gate also runs through Dagger (`dagger call <fn>`), so CI and a laptop run the same thing.
DAGGER ?= dagger
PYTEST_WORKERS ?= 8

help:
	@echo "Targets: install local test lint fmt typecheck check frontend-check app-dev app-e2e fga-test fga-model openfga image NAME=<stem> claude-bootstrap"

# The pinned OpenFGA server the offline suite asserts authorization against (tests/openfga.py), exported from the image
# `.dagger/main.go` pins, so a developer box and `dagger call test` evaluate the model with one server. `dagger core` runs
# from outside the repository so it does not load this repository's module first.
OPENFGA_BIN := $(CURDIR)/.localbin/openfga
OPENFGA_IMAGE = $(shell sed -n 's/^[[:space:]]*OpenfgaImage[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' .dagger/main.go)
FGA_CLI_IMAGE = $(shell sed -n 's/^[[:space:]]*FgaCliImage[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' .dagger/main.go)
AUTH_MODEL = packages/service-kit/src/service_kit/governed/auth
$(OPENFGA_BIN): .dagger/main.go
	@mkdir -p $(dir $@)
	cd /tmp && $(DAGGER) core container from --address=$(OPENFGA_IMAGE) file --path=/openfga export --path=$@
	@chmod +x $@ && touch $@
openfga: $(OPENFGA_BIN)

# The authorization model's own tests, and model.json checked to be model.fga's compiled form.
fga-test:
	$(DAGGER) call fga-test

# Regenerate model.json from model.fga with the pinned CLI, after editing the model.
fga-model:
	cd /tmp && $(DAGGER) core container from --address=$(FGA_CLI_IMAGE) with-directory --path=/auth --source=$(CURDIR)/$(AUTH_MODEL) \
		with-exec --args=/fga,model,transform,--file,/auth/model.fga stdout 2>/dev/null | python3 -m json.tool --sort-keys --indent 2 > $(CURDIR)/.localbin/model.json
	mv $(CURDIR)/.localbin/model.json $(CURDIR)/$(AUTH_MODEL)/model.json

install: sync claude-bootstrap
	prek install --hook-type pre-commit --hook-type commit-msg

sync:
	uv sync --all-packages
	bun --cwd=frontend install

# The workbench on this machine, with real data: install, build the app, seed the demo datasets into a local directory
# catalog, and run the services and the app behind its Gateway stand-in on one origin, on the loopback
# (`scripts/local-stack.sh`, which prints the URL to open, by default http://127.0.0.1:8700/datasets). LOCAL_CATALOG
# names the catalog's directory; EDGE_PORT and the *_PORT variables move the ports, and a port already in use is refused.
LOCAL_CATALOG ?= $(or $(TMPDIR),/tmp)/workbench-catalog
local: sync
	bun --cwd=frontend/app run build
	SEED=1 MEDIA_CATALOG_DIR=$(LOCAL_CATALOG) bash scripts/local-stack.sh

test: $(OPENFGA_BIN)
	uv run pytest -m "not e2e" -n $(PYTEST_WORKERS)

lint:
	uv run ruff check .
	uv run ruff format --check .
	bun --cwd=frontend run lint

fmt:
	uv run ruff format .
	uv run ruff check --fix .
	bun --cwd=frontend run fmt

typecheck:
	uv run ty check
	bun --cwd=frontend run check

check: lint typecheck fga-test
	bun --cwd=frontend run fmt:check
	bun --cwd=frontend run knip

frontend-check:
	bun --cwd=frontend run check
	bun --cwd=frontend run lint
	bun --cwd=frontend run fmt:check
	bun --cwd=frontend run test
	bun --cwd=frontend run knip

# The app (frontend/app) under `vite dev` on :5173: /config.json from WORKBENCH_APP_CONFIG_FILE (else
# frontend/app/dev/config.json), /api proxied to the Gateway at WORKBENCH_EDGE_URL (default :8700, the local stack's edge).
app-dev:
	bun --cwd=frontend/app run dev

# The app's Playwright suite on the BUILT app, behind edge.ts with its mock issuer and services.
app-e2e:
	bun --cwd=frontend/app run test:e2e

# make image NAME=media    → .docker/media.dockerfile, built by Dagger (never docker); NAME=frontend is the app
image:
	$(DAGGER) call image --name=$(NAME)

claude-bootstrap:
	@command -v claude  >/dev/null 2>&1 || { echo "  !! claude CLI not found — install Claude Code first"; exit 1; }
	@command -v bunx    >/dev/null 2>&1 || { echo "  !! bunx not found — install bun first (https://bun.sh)"; exit 1; }
	@echo "==> Svelte MCP server (local scope)..."
	@claude mcp add -t stdio -s local svelte -- bunx -y @sveltejs/mcp || echo "    (already installed — skipping)"
	@echo "==> Marketplaces declared in .claude/settings.json..."
	@python3 -c 'import json; print("\n".join(v["source"]["repo"] for v in json.load(open(".claude/settings.json")).get("extraKnownMarketplaces",{}).values()))' \
		| while read -r repo; do echo "    + $$repo"; claude plugin marketplace add "$$repo" >/dev/null 2>&1 || true; done
	@echo "==> Enabled plugins at project scope..."
	@python3 -c 'import json; print("\n".join(k for k,v in json.load(open(".claude/settings.json")).get("enabledPlugins",{}).items() if v))' \
		| while read -r plugin; do echo "    + $$plugin"; claude plugin install "$$plugin" -s project >/dev/null 2>&1 || true; done
