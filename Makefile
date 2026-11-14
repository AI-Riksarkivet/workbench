.PHONY: help install sync test lint fmt typecheck check frontend-check frontend-fmt image claude-bootstrap

# Every image and gate also runs through Dagger (`dagger call <fn>`), so CI and a laptop run the same thing.
DAGGER ?= dagger
PYTEST_WORKERS ?= 8

help:
	@echo "Targets: install test lint fmt typecheck check frontend-check image NAME=<stem> claude-bootstrap"

install: sync claude-bootstrap
	prek install --hook-type pre-commit --hook-type commit-msg

sync:
	uv sync --all-packages
	bun --cwd=frontend install

test:
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

check: lint typecheck
	bun --cwd=frontend run fmt:check
	bun --cwd=frontend run knip

frontend-check:
	bun --cwd=frontend run check
	bun --cwd=frontend run lint
	bun --cwd=frontend run fmt:check
	bun --cwd=frontend run test

# make image NAME=media    → .docker/media.dockerfile, built by Dagger (never docker)
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
