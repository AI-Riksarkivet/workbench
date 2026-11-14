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
- **Sign-in** is the platform's OIDC provider (Dex in development, Keycloak or the like in production). Neither rask nor
  the workbench hosts identity. The workbench registers its own client and audience there, serves its own login
  callback, and verifies tokens addressed to it.
- **Authorization** is the workbench's own model and store: who may do what to its projects, tasks, corpora and flows.
  It never asks rask's decision door or rask's OpenFGA store, and rask knows nothing of the workbench's users or roles.
- **Data** in a rask lakehouse is rask's to govern. When the workbench reads or writes a table there, rask decides
  whether that caller (the person, or the workbench's service identity) may. The workbench is one more outside client
  there.

Not there yet: service-kit's `governed/decider.py` still asks rask's decision door, and its OpenFGA setup still
provisions rask's authorization model. Replacing both with the workbench's own is spec row WB-5.

It imports no rask code and shares none with it. What it once imported from rask is its own copy here, under its own
names, changed freely: service-kit and storage, and the frontend packages `@workbench/ui`, `@workbench/api`,
`@workbench/config`, `@workbench/dockview` and `@workbench/flow`. Its settings are `WORKBENCH_*` environment variables
(the `LANCE_*` ones name what it asks of a lakehouse). rask's repository is `AI-Riksarkivet/rask`;
compute (Ray Serve, the runners) is a repository of its own, called over HTTP.

The code came out of rask's history at `64ef118a9` on 2026-11-14. The move and what remains to do are on issue #1
(the spec, rows WB-1 onward).

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

## Toolchain rules

- **NEVER DOCKER. Dagger builds every image and runs every gate.** No `docker build`, `docker run`, `docker compose`,
  `docker ps` or any other docker command, for any purpose. `dagger call image --name=<stem>` builds
  `.docker/<stem>.dockerfile`; an ad-hoc container is `dagger core container from --address=<img> ...`.
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
| Frontend gates | `make frontend-check` |
| Build an image | `make image NAME=<stem>` (= `dagger call image --name=<stem>`) |
| The CI gates locally | `dagger call lint`, `dagger call test`, `dagger call frontend-check` |

## Repository layout

Two language-pure planes, as in rask; never blur them.

- `packages/`: reusable Python libraries, no entrypoints. `service-kit` (the service base: app factory, governed
  auth, settings), `storage` (the S3 client seam), `client-kit` (the media app factory and dataset registry the
  viewer, search and annotator share).
- `services/`: runnable Python. `viewer`, `search`, `annotator`, `flows`, each with its own `tests/`.
- `runners/assist`: the annotator's assist model server, a sealed environment with its own `uv.lock`; not a workspace
  member, so its model pins never enter the services' resolution.
- `frontend/`: the bun workspace root (`package.json`, `bun.lock`, `knip.json`, oxlint/oxfmt config, `patches/`).
  - `frontend/explorer`, `frontend/annotator`, `frontend/studio`: the three SvelteKit apps.
  - `frontend/packages/*`: their libraries, `@workbench/*` (`ui`, `api`, `config`, `dockview`, `flow`, `explorer-api`,
    `engine`, `labeling`). `@workbench/ui` is read from source: its exports name `src/`, with no build step.
- `.docker/<stem>.dockerfile`: one per deployable (`media` = viewer + search, `annotator`, `flows`, `assist-runner`),
  built from the repository root against the root `uv.lock`.
- `.dagger/`: the Dagger module (`image`, `lint`, `test`, `frontend-check`).
- `deploy/fixtures/annotator`: the annotator's development fixture.
- `docs/rask/`: rask's published contract, vendored (`PROVENANCE.md` says from which sha and how to refresh it).
- `scripts/`: dev and ops scripts.

Workspace membership is globbed: `[tool.uv.workspace] members = ["packages/*", "services/*"]` and
`frontend/package.json` `workspaces = ["explorer", "annotator", "studio", "packages/*"]`. A new Python member must ship a
`pyproject.toml` (uv refuses the workspace otherwise); a new JS package must ship a `package.json` (bun silently skips
it otherwise). Add a new Python import name to ruff's `known-first-party` in the same change.

## Git

- Commit messages are Conventional Commits (the commit-msg hook checks it) and carry no `Co-Authored-By: Claude` line
  (the hook refuses it).
