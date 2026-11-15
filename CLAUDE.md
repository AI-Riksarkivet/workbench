# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**The workbench: explore, search and annotate the datasets in a rask lakehouse, and run model pipelines over them**, as
one product (FiftyOne-like). Its parts:

- **explorer**: the viewer, search and the explorer UI;
- **annotator**: the annotator, its assist runner and its UI;
- **flows**: model pipelines and the studio UI.

**It is a client of rask, never part of it.** It reaches a lakehouse only through what rask publishes, the way any outside
tool would:

- data through stock `lance_namespace` and pylance, on the credentials the catalog vends;
- every other call through the catalog's and lineage's doors (`docs/rask/*-openapi.json`);
- the lakehouse's events as `docs/rask/COMMIT-EVENTS.md` describes them.

**Its authentication and authorization are its own and have nothing to do with rask's.**
- **Sign-in** is any OIDC provider (Keycloak is the reference; Keycloak replaces Dex in development with rask's XC-214).
  Neither rask nor the workbench hosts identity. The workbench has its own public PKCE client and its own audience
  there. Its tokens are bound to the browser's key with DPoP (RFC 9449), and every service verifies the token and its
  proof on every request (`service_kit.governed.deps`, `service_kit.governed.dpop`).
- **Authorization** is the workbench's own OpenFGA model and store (`service_kit/governed/auth/model.fga`): an org's
  admins and members, a project's five roles (owner > manager > reviewer > annotator > viewer), and the tasks, corpora
  and flows that inherit from a project. It never asks rask's decision door or rask's OpenFGA store, and rask knows
  nothing of the workbench's users or roles. The model and the declared organisations are written by the model writer
  (`python -m service_kit.governed.auth.write_model`), a Job the deployment runs; a service finds the store by its name
  and the model its own image carries, and never writes either.
- **Data** in a rask lakehouse is rask's to govern. A read is made as the person, on a token the service exchanges for
  them at the provider (RFC 8693, `service_kit.governed.token_exchange`): the person's own token is never forwarded.
  An annotation write is made as the workbench's own confidential client after the workbench's own role check on the
  project the request names (`?project=`), into that project's own table in the workbench's annotations namespace
  (`<project>__<dataset digest>`), and names the person in the commit; a corpus binding grants reading, never writing.
  rask decides whether that caller may, and its refusal is final.

The settings, one `WORKBENCH_*` name each (`service_kit.governed.settings`); every credential is a mounted file:

| Setting | What it names |
| --- | --- |
| `WORKBENCH_OIDC_ENABLED`, `_ISSUER`, `_AUDIENCE`, `_IDP_ID`, `_DISCOVERY_URL` | sign-in, on by default; the provider, the workbench's own audience, and the idp id its subjects are principals of (`<idp-id>~<sub>`) |
| `WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED` | the only way to run with sign-in off, refused unless sign-in is off; a service with it logs a warning at start |
| `WORKBENCH_PUBLIC_URL`, `WORKBENCH_PUBLIC_MOUNT`, `WORKBENCH_DPOP_WINDOW_SECONDS` | the URL a DPoP proof's `htu` names (and the path a gateway rewrites it to), and the proof's `iat` and replay window |
| `WORKBENCH_OIDC_CLIENT_ID`, `WORKBENCH_OIDC_CLIENT_SECRET_FILE`, `WORKBENCH_LAKEHOUSE_AUDIENCE` | the workbench's confidential client, which exchanges a person's token and takes its own, both addressed to the lakehouse |
| `ANNOTATOR_ANNOTATIONS_NAMESPACE` | the lakehouse namespace that holds every project's annotation tables (`workbench$annotations`) |
| `WORKBENCH_FGA_ENABLED`, `_API_URL`, `_STORE`, `_MODEL_ID`, `_CREDENTIAL_FILE` | the workbench's own OpenFGA store, found by name, its model by the body the image carries unless pinned |
| `VIEWER_MEDIA_URL_KEY_FILE` | a credential: the mounted file holding the viewer's media-URL signing key, base64 of at least 32 random bytes (`openssl rand -base64 32`), read on every use; unset or short, the signing door answers 503 |
| `VIEWER_MEDIA_URL_TTL_SECONDS` | how long a signed media URL serves its cell: default 300, from 10 to 3600 |
| `WORKBENCH_FLOWS_SERVE_TOKEN_FILE` | a credential: the mounted projected ServiceAccount token (audience `serve`) flows presents to Ray Serve, read on every call; unset, every call to Serve is refused |

It imports no rask code and shares none with it. What it once imported from rask is its own copy here, under its own
names, changed freely: service-kit, and the frontend packages `@workbench/ui`, `@workbench/api`,
`@workbench/config` and `@workbench/flow`. Its settings are `WORKBENCH_*` environment variables
(the `LANCE_*` ones name what it asks of a lakehouse). rask's repository is `AI-Riksarkivet/rask`;
compute (Ray Serve, the runners) is a repository of its own, called over HTTP.

The code came out of rask's history at `64ef118a9` on 2026-11-14. The move and what remains to do are on issue #1
(the spec, rows WB-1 onward).

## Where the work stands

The open rows are issues #6 to #10 (WB-A to WB-E), each with its decisions in its body and comments. #9 (WB-D, the
one app) is split into steps, and each landed step has a comment saying what landed, the gates, what was not
confirmed and the next step: **read #9's latest comment before starting.** A row's live closes-when waits for rask's
XC-214 (`AI-Riksarkivet/rask#829`, Keycloak for token exchange); until then the app is proven against the e2e mocks.
Assign yourself the row you take (`gh issue edit <n> --add-assignee @me`).

## Engineering principles (read first)

**State-of-the-art work. Do things properly: no shortcuts, no band-aids.**

- **Fix root causes, not symptoms.** Never paper over an app or library bug at an outer layer (proxy, ingress, env hack,
  wrapper). A workaround is at most a clearly labelled temporary step, replaced by the real fix.
- **Verify like it ships.** A 200 from server rendering is not "working": exercise the real user path (a browser for UI,
  the actual client for APIs) and observe it end to end.
- **Plan before editing, and test first.** A non-trivial change gets a plan you can state, a RED test that proves the
  defect, then the fix.
- **Tests: invoke the testing skills first, every time.** Before writing, changing or deleting a test, invoke
  `testing-python` and read `writing-python`'s `references/testing.md` (and `fastapi`'s testing reference for a FastAPI
  door). Integration tests through real collaborators (the app over HTTP, real pylance, moto, a real OpenFGA) are the
  keepers; a unit test earns its place only for a pure function's distinct branches. No test reads source text, an
  AST, prose or file names: a code-shape rule belongs in ruff or nowhere. State the net test delta in each commit.
- **No silent scope-cuts.** Say what you bounded, sampled or deferred.
- **No backward compatibility before production.** No shims, dual paths or deprecation windows: change the thing and
  move every consumer in the same commit.
- **Lance-idiomatic, answered from rask's `lance_docs/` or a measurement on the installed pylance, never from memory.**
- **Four facts judge every design, GitOps first: GitOps, cloud native, Lance-namespace/table-idiomatic, zero trust.**
  GitOps: the release is what git declares, reconciled with no imperative step. Cloud native: platform services are
  named, never bundled; every component is a declarative Kubernetes workload. Lance-idiomatic: data through
  lance-namespace's doors and Lance's own primitives. Zero trust: no identity trusted by position, no secret through
  env, every credential scoped and short-lived. Name the fact each option meets or breaks before offering it.
- **Python follows the ra-skills `writing-python` and `fastapi` references, read before writing:** Pydantic-only
  structured models, `TypeError` for a wrong type, no `# type: ignore` / `# ty: ignore` (narrow or cast).
- **Owner decisions go through the multi-question tool**, each option with a concrete what, how and why. Research
  before asking: what the code, a measurement or the docs can answer is decided, with the evidence reported.
- **Stopping rules.** When a step needs no owner input, keep going; put status in the same message as the next action.
  Stop and ask only when you cannot continue without the owner, or before anything destructive or outward-facing.
- **Comments carry rationale and provenance, never history.** "This used to say X" belongs in the commit message.
- **Every canvas draws with WebGPU only** (#10): pixi with `preference: ['webgpu']` (an array; a string falls back to
  WebGL), hand-written WebGPU in `#lib/gpu`, and the plain "WebGPU is required" state where it is missing.
- **A step lands as one reviewed commit on `main`.** Two reviewers read it in parallel, one for standards and security
  and one for the spec and usability (a browser walk with `playwright-cli` for UI), and every finding in the step's
  own code is fixed, LOWs included. Then the gates (`dagger call frontend-check`, `dagger call app-end-to-end` at
  0 failed and 0 flaky, the full `make test`), one commit with the tests delta, a push, and a comment on the row.

## Toolchain rules

- **NEVER DOCKER. Dagger builds every image and runs every gate.** No `docker build`, `docker run`, `docker compose`,
  `docker ps` or any other docker command, for any purpose. `dagger call image --name=<stem>` builds
  `.docker/<stem>.dockerfile`; an ad-hoc container is `dagger core container from --address=<img> ...`.
  - **On a host that runs a named engine** (the owner's runs `dagger-engine-rask`, shared with rask), prefix every
    `dagger` command with `_EXPERIMENTAL_DAGGER_RUNNER_HOST=docker-container://dagger-engine-rask`: an unscoped CLI
    removes engines named `dagger-engine-*` and breaks every session using it.
  - **`dagger call app-end-to-end` uploads the working tree, not the index**, and two Playwright runs at once void
    each other: run one at a time (a shared `flock`), and never while a test's red check has the code broken.
- **JS/TS uses bun exclusively** (`bun`, `bunx`; no npm, npx or pnpm). Every call is scoped to the frontend root:
  `bun --cwd=frontend run <task>` (the `--cwd=` form; with a space it silently does nothing).
- **JS/TS lint and format are oxlint and oxfmt** (`frontend/.oxlintrc.json`, `frontend/.oxfmtrc.json`); tabs, single
  quotes, print width 100.
- **Python uses uv (3.13), Ruff and ty.** `uv run <cmd>`; type-check with `uv run ty check` (the locked ty). Ruff line
  length is 160; `ty` fails on a warning.

## Common commands

| Goal | Command |
| --- | --- |
| First-time setup | `make install` (uv sync, bun install, prek hooks, Claude Code plugins) |
| Tests (no live stack) | `make test` (= `uv run pytest -m "not e2e" -n 8`) |
| One test | `uv run pytest services/<svc>/tests/<file>.py::<test>` |
| Lint, format, type-check | `make check` |
| The authorization model's tests | `make fga-test` (= `dagger call fga-test`); after editing `model.fga`, `make fga-model` |
| The pinned OpenFGA server for the suite | `make openfga` (exported through Dagger to `.localbin/openfga`) |
| Frontend gates | `make frontend-check` (check, lint, fmt:check, vitest, knip); the app's Playwright suite `make app-e2e`, or `dagger call app-end-to-end` |
| Build an image | `make image NAME=<stem>` (= `dagger call image --name=<stem>`) |
| The CI gates locally | `dagger call lint`, `dagger call test`, `dagger call frontend-check`, `dagger call app-end-to-end` |

## Repository layout

Two language-pure planes, as in rask; never blur them.

- `packages/`: reusable Python libraries, no entrypoints. `service-kit` (the service base: app factory, governed
  auth, settings) and `client-kit` (the media app factory and dataset registry the viewer, search and annotator
  share). Every dataset is a catalog namespace opened through lance-namespace as the caller, on the credentials the
  catalog vends: the workbench holds no store key.
- `services/`: runnable Python. `viewer`, `search`, `annotator`, `flows`, each with its own `tests/`.
  - The viewer serves media to `<img>` and `<video>`, which send no token, through short signed URLs
    (`viewer.api.v1.endpoints.signed_media`): `POST /api/signed-media` (DPoP) resolves each cell by the person's own
    read and signs it with the key in `VIEWER_MEDIA_URL_KEY_FILE`; `GET /api/signed-media/{token}` reads exactly that
    cell, at that version, as the viewer's own identity (the link is the permission), sandboxed and immutable.
- `runners/assist`: the annotator's assist model server, a sealed environment with its own `uv.lock`; not a workspace
  member, so its model pins never enter the services' resolution.
- `frontend/`: the bun workspace root (`package.json`, `bun.lock`, `knip.json`, oxlint/oxfmt config, `patches/`).
  - `frontend/app`: THE app (#9): one browser-only SvelteKit 3 single-page app (adapter-static, `ssr = false`, no
    server code), served by its own `serve.ts` with the strict CSP as a header. It signs in with PKCE on the
    workbench's public client (oidc-client-ts), binds the tokens to the tab with DPoP (an in-memory, non-extractable
    P-256 key), and calls each service same-origin at the public base `/config.json` names (`services`), which is also
    what every proof's `htu` names. Its data layer is `#lib/reads.svelte.ts`; its fetch seam is `#lib/api/client.ts`,
    which `@workbench/explorer-api` also calls through (`#lib/api/explorer.ts` installs it); every media element's
    link comes from one batched loader (`#lib/api/media-links.svelte.ts`); its explorer is `#lib/explore`; its dock is
    `#lib/dock` (horizon-layout 1.0.5, `patches/horizon-layout@1.0.5.patch`). A dataset's pages are its dock
    (`/datasets/[id]`) and its views (`#lib/dataset-nav.ts`): the atlas (`#lib/atlas`, also the dock's Atlas panel), the
    knowledge graph (`#lib/graph`) and the topic tree (`#lib/topics`). The atlas and the graph draw with WebGPU only
    (`#lib/gpu`); a browser without it gets their plain refusal and fetches no points. The annotator's project plane
    is `/projects` and `/projects/[project]` (`#lib/projects`, its calls `#lib/api/projects.api.ts`): a project's
    read answers the reader's `permissions`, and a page offers only what its door would let through; a dataset's
    search results and atlas selection are sent to a project from `#lib/projects/SendToProjectDialog.svelte`. A task's labeling canvas is `/projects/[project]/tasks/[task]` (`#lib/canvas`: the engine, folded in from `@workbench/engine`, with `pixi.js/unsafe-eval` for the CSP; WebGPU only; its annotations, tags and versions at the annotator's `/api/annotations/<unit key>` with `?project=`, `?task=` and `?dataset=`, `#lib/api/annotations.api.ts`, where the door reads the unit against the task's own record and takes an edit only from the claim's holder or a reviewer of work in review, `annotator.annotations.access`). Image
    `.docker/frontend.dockerfile`; released by `deploy/fixtures/annotator` (`app.*`) at `/`.
  - `frontend/explorer`: only its `/workflow` editor and what that needs (its layout, `lib/workflow`, the atlas map
    its Atlas node opens, the player), until D4 merges it with studio's flows. `frontend/annotator`, `frontend/studio`:
    the old SvelteKit 2 apps, moving into `frontend/app` (#9 D3, D4); never deployable. The annotator's project pages
    and its image canvas are in the app (D3a, D3b-1); its temporal and text viewers, compare and assist follow (D3b-2), then its data manager and browse (D3c), which deletes it and `@workbench/engine`, kept until then for it alone.
  - `frontend/packages/*`: their libraries, `@workbench/*` (`ui`, `api`, `config`, `flow`, `explorer-api`,
    `engine`, `labeling`). `@workbench/ui` is read from source: its exports name `src/`, with no build step.
- `.docker/<stem>.dockerfile`: one per deployable (`media` = viewer + search, `annotator`, `flows`, `assist-runner`, `frontend` = the app),
  built from the repository root against the root `uv.lock`.
- `.dagger/`: the Dagger module (`image`, `lint`, `test`, `frontend-check`, `app-end-to-end`, `fga-test`).
- `deploy/fixtures/annotator`: the annotator's development fixture.
- `docs/rask/`: rask's published contract, vendored (`PROVENANCE.md` says from which sha and how to refresh it).
- `scripts/`: dev and ops scripts.

Workspace membership is globbed: `[tool.uv.workspace] members = ["packages/*", "services/*"]` and
`frontend/package.json` `workspaces = ["app", "explorer", "annotator", "studio", "packages/*"]`. A new Python member must ship a
`pyproject.toml` (uv refuses the workspace otherwise); a new JS package must ship a `package.json` (bun silently skips
it otherwise). Add a new Python import name to ruff's `known-first-party` in the same change.

## Git

- Commit messages are Conventional Commits (the commit-msg hook checks it) and carry no `Co-Authored-By: Claude` line
  (the hook refuses it).
