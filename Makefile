.PHONY: help install sync local local-stop test lint fmt typecheck check frontend-check frontend-fmt app-dev app-e2e image claude-bootstrap openfga postgres fga-test fga-model model-protocol chart-check \
	deploy-dev undeploy-dev dev-clean-tree dev-images dev-postgres dev-openfga dev-model-stand-in dev-release dev-readback \
	dev-undeploy-release dev-undeploy-model-stand-in dev-undeploy-openfga dev-undeploy-postgres dev-undeploy-namespace

# Every image and gate also runs through Dagger (`dagger call <fn>`), so CI and a laptop run the same thing.
DAGGER ?= dagger
# A host's own settings, in a file git does not track. A host that shares one Dagger engine between repositories names it
# here, `export _EXPERIMENTAL_DAGGER_RUNNER_HOST := docker-container://<engine>`, so every target's Dagger call reaches
# that engine: a call with no engine named starts the CLI's own and can remove the shared one.
-include local.mk
PYTEST_WORKERS ?= 8

help:
	@echo "Targets: install local local-stop test lint fmt typecheck check chart-check frontend-check app-dev app-e2e fga-test fga-model openfga postgres image NAME=<stem> claude-bootstrap"
	@echo "Dev cluster (docs/DEPLOY.md): deploy-dev undeploy-dev, or one step at a time: dev-images dev-postgres dev-openfga dev-model-stand-in dev-release dev-readback"

# The pinned OpenFGA server the offline suite asserts authorization against (tests/openfga.py), exported from the image
# `.dagger/main.go` pins, so a developer box and `dagger call test` evaluate the model with one server. `dagger core` runs
# from outside the repository so it does not load this repository's module first.
OPENFGA_BIN := $(CURDIR)/.localbin/openfga
OPENFGA_IMAGE = $(shell sed -n 's/^[[:space:]]*OpenfgaImage[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' .dagger/main.go)
FGA_CLI_IMAGE = $(shell sed -n 's/^[[:space:]]*FgaCliImage[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' .dagger/main.go)
AUTH_MODEL = packages/workbench/src/workbench/governed/auth
$(OPENFGA_BIN): .dagger/main.go
	@mkdir -p $(dir $@)
	cd /tmp && $(DAGGER) core container from --address=$(OPENFGA_IMAGE) file --path=/openfga export --path=$@
	@chmod +x $@ && touch $@
openfga: $(OPENFGA_BIN)

# The pinned PostgreSQL server the offline suite (tests/postgres.py) and the local stack run the annotator's state on,
# exported from the image `.dagger/main.go` pins as a relocatable tree (`scripts/postgres-server.sh`), so a developer
# box and `dagger call test` run one server.
POSTGRES_DIR := $(CURDIR)/.localbin/postgres
POSTGRES_IMAGE = $(shell sed -n 's/^[[:space:]]*PostgresImage[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' .dagger/main.go)
$(POSTGRES_DIR)/bin/postgres: .dagger/main.go scripts/postgres-server.sh
	rm -rf $(POSTGRES_DIR)
	cd /tmp && $(DAGGER) core container from --address=$(POSTGRES_IMAGE) \
		with-file --path=/postgres-server.sh --source=$(CURDIR)/scripts/postgres-server.sh \
		with-exec --args=sh,/postgres-server.sh directory --path=/out export --path=$(POSTGRES_DIR)
	@touch $@
postgres: $(POSTGRES_DIR)/bin/postgres

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

# The workbench on this machine, with real data: install, build the app, seed the demo sources (a directory catalog
# standing in for a lakehouse, a lance-namespace directory catalog and a bare table, `scripts/local-sources.json`), and run the services and the app behind its Gateway stand-in on one origin, on the loopback
# (`scripts/local-stack.sh`, which prints the URL to open, by default http://127.0.0.1:8700/datasets). LOCAL_CATALOG
# names the root they live under; EDGE_PORT and the *_PORT variables move the ports, and a port already in use is refused.
# The ports are checked first, before anything is installed or built: a build replaces the files a running stack serves.
LOCAL_CATALOG ?= $(or $(TMPDIR),/tmp)/workbench-catalog
local:
	bash scripts/local-stack.sh check
	$(MAKE) sync postgres
	bun --cwd=frontend/app run build
	SEED=1 LOCAL_CATALOG=$(LOCAL_CATALOG) bash scripts/local-stack.sh

# Stop the local stack `make local` started (the process its PID file names), and wait until its ports are free.
local-stop:
	bash scripts/local-stack.sh stop

test: $(OPENFGA_BIN) $(POSTGRES_DIR)/bin/postgres
	uv run pytest -m "not e2e" -n $(PYTEST_WORKERS) --dist loadgroup

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run lint-imports
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

# The chart renders and lints in each shape a deployment takes: the development estate's overlay (with its demo volume,
# the Job that fills it and the source reading it at a path on its claim, `demo`), a platform's (an https
# IdP that binds tokens, a rask lakehouse by reference), a release with no lakehouse at all (a directory catalog on a
# claim and a bare table on a store), and one with sign-in off reading one table on a claim.
CHART_APIS = --api-versions generators.external-secrets.io/v1alpha1/Password
CHART_STATE = --set state.host=pg.example --set state.password.name=wb-state --set state.ca.name=pg-ca
CHART_PLATFORM = --set auth.oidc.issuer=https://id.example --set auth.oidc.idpId=corp --set auth.oidc.publicOrigin=https://wb.example \
	--set auth.oidc.client.secret.name=wb-client --set auth.lakehouse.audience=lakehouse --set 'sources[0].name=lake' --set 'sources[0].kind=rask' --set 'sources[0].uri=https://lakehouse.example' \
	--set auth.fga.apiUrl=https://fga.example --set media.viewer.mediaUrlKey.name=wb-media --set 'route.parentRefs[0].name=gw'
CHART_NO_LAKEHOUSE = --set auth.oidc.issuer=https://id.example --set auth.oidc.idpId=corp --set auth.oidc.publicOrigin=https://wb.example \
	--set 'sources[0].name=archive' --set 'sources[0].kind=namespace' --set 'sources[0].impl=dir' --set 'sources[0].claim=archive' --set 'sources[0].annotatable=true' \
	--set 'sources[0].readers.orgs[0]=acme' --set 'sources[0].readers.people[0]=corp~ada' \
	--set 'sources[1].name=scans' --set 'sources[1].kind=tables' --set 'sources[1].uri=s3://bucket/scans.lance' \
	--set 'sources[1].storage.region=eu-north-1' --set 'sources[1].storage.aws_role_arn=arn:aws:iam::1:role/scans' \
	--set 'sources[1].storage.aws_web_identity_token_file=/var/run/secrets/workbench/store/token' --set storeIdentity.audience=sts.amazonaws.com \
	--set auth.fga.apiUrl=https://fga.example --set media.viewer.mediaUrlKey.name=wb-media \
	--set 'route.parentRefs[0].name=gw'
# The model protocol's OpenAPI document, written from the stand-in endpoint that speaks it (docs/model-protocol.md).
model-protocol:
	uv run python -m annotator.model_endpoint.stand_in --openapi > docs/model-protocol-openapi.json

chart-check:
	uv run python -m workbench.source_keys | diff - chart/files/source-keys.json
	helm lint chart -f deploy/values-dev.yaml
	helm template workbench chart -n workbench $(CHART_APIS) -f deploy/values-dev.yaml >/dev/null
	helm template workbench chart -n workbench $(CHART_APIS) $(CHART_STATE) $(CHART_PLATFORM) >/dev/null
	helm template workbench chart -n workbench $(CHART_APIS) $(CHART_STATE) $(CHART_NO_LAKEHOUSE) >/dev/null
	helm template workbench chart -n workbench $(CHART_APIS) $(CHART_STATE) --set auth.enabled=false --set route.enabled=false \
		--set 'sources[0].name=scans' --set 'sources[0].kind=tables' --set 'sources[0].claim=scans' --set 'sources[0].path=scans.lance' \
		--set media.viewer.mediaUrlKey.name=wb-media >/dev/null
	@# A Job's pod template cannot change in place: a change to any field of the demo seed's must name a new Job.
	@seed() { helm template workbench chart -n workbench $(CHART_APIS) -f deploy/values-dev.yaml --set media.image.pullPolicy=$$1 \
		| grep -o 'workbench-demo-seed-[0-9a-f]\{10\}' | sort -u; }; \
		test "$$(seed IfNotPresent)" != "$$(seed Always)" || { echo "the demo seed Job keeps its name when its pod template changes" >&2; exit 1; }
	bash scripts/chart-refusals.sh
	helm lint deploy/fixtures/postgres/chart
	helm template workbench-postgres deploy/fixtures/postgres/chart -n workbench $(CHART_APIS) >/dev/null
	helm lint deploy/fixtures/openfga/chart
	helm lint deploy/fixtures/model-stand-in/chart --set image.tag=dev
	helm template workbench-model-stand-in deploy/fixtures/model-stand-in/chart -n workbench --set image.tag=dev >/dev/null
	helm template workbench-openfga deploy/fixtures/openfga/chart -n workbench $(CHART_APIS) >/dev/null
	@! helm template workbench-openfga deploy/fixtures/openfga/chart -n workbench $(CHART_APIS) --set database.host= >/dev/null 2>&1 \
		|| { echo "the OpenFGA fixture renders with no database host" >&2; exit 1; }

# ── The development cluster (docs/DEPLOY.md) ──────────────────────────────────────────────────────────────────────────
# One entry point, `make deploy-dev`, and each step its own target, each safe to run again. Values come from git alone
# (`deploy/values-dev.yaml` and the commit's image tags, with --reset-values): nothing is read back from a release.
# Every dagger call names the shared engine, since an unscoped CLI removes engines named `dagger-engine-*`.
DEV_NAMESPACE ?= workbench
DEV_RELEASE ?= workbench
DEV_POSTGRES_RELEASE ?= workbench-postgres
DEV_OPENFGA_RELEASE ?= workbench-openfga
DEV_MODEL_STAND_IN_RELEASE ?= workbench-model-stand-in
DEV_REGISTRY_PUSH ?= 172.17.0.1:5000
DEV_ENGINE ?= docker-container://dagger-engine-rask
DEV_TAG = $(shell git rev-parse --short=12 HEAD)
DEV_GATEWAY_HOST ?= workbench.localhost
DEV_HELM = helm --namespace $(DEV_NAMESPACE)

# What the workbench asks of the lakehouse's estate is not installed from here: its sign-in clients at the development
# Dex (`deploy/dex-client.yaml`) and its own reader grant (`deploy/lakehouse-client.yaml`) are values the lakehouse's
# install is handed (docs/DEPLOY.md), so no target writes to Dex or to the catalog.
deploy-dev: dev-clean-tree dev-images dev-postgres dev-openfga dev-model-stand-in dev-release dev-readback

# Images are named by the commit they are built from, so the tree must be that commit.
dev-clean-tree:
	@test -z "$$(git status --porcelain)" || { echo "the working tree is not clean: images are tagged by the commit ($(DEV_TAG)); commit or stash first" >&2; exit 1; }

# media (the viewer and search), the annotator and the app, published to the development registry the cluster pulls from
# as localhost:5000 (the bridge address here, since the engine is itself a container).
dev-images: dev-clean-tree
	_EXPERIMENTAL_DAGGER_RUNNER_HOST=$(DEV_ENGINE) $(DAGGER) call image --name=media publish --address=$(DEV_REGISTRY_PUSH)/workbench-media:$(DEV_TAG)
	_EXPERIMENTAL_DAGGER_RUNNER_HOST=$(DEV_ENGINE) $(DAGGER) call image --name=annotator publish --address=$(DEV_REGISTRY_PUSH)/workbench-annotator:$(DEV_TAG)
	_EXPERIMENTAL_DAGGER_RUNNER_HOST=$(DEV_ENGINE) $(DAGGER) call image --name=frontend publish --address=$(DEV_REGISTRY_PUSH)/workbench-app:$(DEV_TAG)

# The development PostgreSQL (deploy/fixtures/postgres), standing in for a platform's: the annotator's state and the
# development OpenFGA's tuples are databases on it, so it comes before both.
dev-postgres:
	$(DEV_HELM) upgrade --install $(DEV_POSTGRES_RELEASE) deploy/fixtures/postgres/chart --create-namespace --reset-values \
		--description "workbench fixture: not for production" --wait --timeout 10m

# The workbench's own OpenFGA (deploy/fixtures/openfga), before the release: its model writer runs against it.
dev-openfga:
	$(DEV_HELM) upgrade --install $(DEV_OPENFGA_RELEASE) deploy/fixtures/openfga/chart --create-namespace --reset-values \
		--description "workbench fixture: not for production" --wait --timeout 10m

# The stand-in model endpoint (deploy/fixtures/model-stand-in), from the annotator's image at this commit: what the
# release's `models.endpoint.url` names in development.
dev-model-stand-in:
	$(DEV_HELM) upgrade --install $(DEV_MODEL_STAND_IN_RELEASE) deploy/fixtures/model-stand-in/chart --create-namespace --reset-values \
		--set-string image.tag=$(DEV_TAG) --description "workbench fixture: not for production" --wait --timeout 5m

dev-release:
	$(DEV_HELM) upgrade --install $(DEV_RELEASE) chart --create-namespace --reset-values -f deploy/values-dev.yaml \
		--set-string image.tag=$(DEV_TAG) --set-string media.image.tag=$(DEV_TAG) --set-string app.image.tag=$(DEV_TAG) \
		--wait --wait-for-jobs --timeout 15m

# Every workload Ready, and the route answering the app's own /config.json through the Gateway under the workbench's host.
dev-readback:
	kubectl --namespace $(DEV_NAMESPACE) wait --for=condition=Available deployment --all --timeout=300s
	kubectl --namespace $(DEV_NAMESPACE) rollout status statefulset/$(DEV_POSTGRES_RELEASE) --timeout=300s
	@gateway="$$(kubectl --namespace kube-system get gateway traefik-gateway -o jsonpath='{.status.addresses[0].value}')"; \
		curl -fsS -H 'Host: $(DEV_GATEWAY_HOST)' "http://$$gateway/config.json" >/dev/null \
		&& echo "the workbench answers at http://$(DEV_GATEWAY_HOST):8080 (through the port-forward to the Gateway)"

undeploy-dev: dev-undeploy-release dev-undeploy-model-stand-in dev-undeploy-openfga dev-undeploy-postgres dev-undeploy-namespace
	@echo "left: the images workbench-media, workbench-annotator and workbench-app in the development registry, and what the lakehouse's install carries for it (docs/DEPLOY.md)"

dev-undeploy-release:
	$(DEV_HELM) uninstall $(DEV_RELEASE) --ignore-not-found --wait

dev-undeploy-model-stand-in:
	$(DEV_HELM) uninstall $(DEV_MODEL_STAND_IN_RELEASE) --ignore-not-found --wait

dev-undeploy-openfga:
	$(DEV_HELM) uninstall $(DEV_OPENFGA_RELEASE) --ignore-not-found --wait

# The claim with the release: the passwords are made again at the next install, and the server takes them only when it
# initialises an empty data directory.
dev-undeploy-postgres:
	$(DEV_HELM) uninstall $(DEV_POSTGRES_RELEASE) --ignore-not-found --wait
	kubectl --namespace $(DEV_NAMESPACE) delete persistentvolumeclaim data-$(DEV_POSTGRES_RELEASE)-0 --ignore-not-found --wait

# The namespace last.
dev-undeploy-namespace:
	kubectl delete namespace $(DEV_NAMESPACE) --ignore-not-found --wait

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
