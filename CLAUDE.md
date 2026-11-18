# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**The workbench: explore, search and annotate the datasets in a rask lakehouse, read any Lance data beside it, and run
model pipelines over them**, as one product (FiftyOne-like). Its parts:

- **explorer**: the viewer, search and the explorer UI;
- **annotator**: the annotator, its assist runner and its UI;
- **flows**: model pipelines and the studio UI.

**It reads the sources a deployment declares, and is a client of rask, never part of it** (owner, 2026-11-16: "as a client
for rask, or without rask with lance-namespace or a Lance table directly"). The sources are one declaration in git
(`client_kit.sources`; the chart's `sources`, the local stack's `scripts/local-sources.json`), each a name and a kind:
`rask` (a lakehouse by its catalog's REST door, at most one), `namespace` (any lance-namespace implementation and its
properties, through `lance_namespace.connect`) or `tables` (a bare `.lance` table or a directory of them, read as a V1
directory catalog with the manifest off). A dataset is a namespace of a source that holds tables, at any depth, and its
id is `<source>~<namespace>` (`lake~acme$corpus`, `scans~`). **Everything rask-only sits behind the `rask` kind** and
nothing else: the person's lakehouse token (never sent to another source), the `x-lance-originator` and `x-rask-*`
headers, the warehouse-first id rule, and publish. Every other source is read-only and read with no
token, through `client_kit.lancekit.confined.ConfinedNamespace` (only the names it lists, only under its declared
locations, on its declared storage options). **What is labelled** (owner, 2026-11-17: "Only if marked"): a lakehouse's
datasets always; any other source's only when git marks it `annotatable: true` (`client_kit.sources.annotatable`, the
one function the send door, `annotator.corpus.resolve`, the default-dataset check and the app's send gate read, through
the descriptor's `read_only` reason "source '<name>' is not marked annotatable"). Its items record their table and
version at send like a lakehouse's, and a publish names only the lakehouse's tables as lineage inputs while every published row names its item's table and
version (`item_table`, `item_table_version`). **Work in progress is the annotator's own** (owner, 2026-11-17: "Postgres
until publish"): the labels people make live only in its Postgres, in each task's draft, until the project is published;
the lakehouse is written once, at publish, through the catalog's doors. So labeling needs no lakehouse at all (a
deployment with no `rask` source labels any source marked annotatable, and its publish says plainly there is nowhere to
publish to), and the workbench holds no write grant in a lakehouse while people label. **Who reads what:** a lakehouse read as the person decides every read itself; every other
source, and a lakehouse read as the workbench (`identity: service`), is decided by the workbench's own OpenFGA, `reader`
on `source:<name>` from the source's `readers` in git (`client_kit.media.source_gate`; none means nobody), which the
viewer, search and the annotator each ask: the request's caller carries the answer (`source_gate.admit`) and every read
checks it as it resolves its source (`client_kit.sources.require_admitted`). A service that signs people in refuses to
start beside such a source without that store, and beside a `rask` directory stand-in read as nobody
(`client_kit.media.config.GovernedMediaSettings`), so only sign-in off lets every check allow. No source
names a credential: properties and storage options are an allow-list (`client_kit.source_keys`, the chart's copy in
`chart/files/source-keys.json`), and every id segment a request names is checked (`client_kit.source_names`). A dataset with no `lance_media.descriptor` (or an invalid one) is shown from its
schema as a plain table (`viewer.services.plain_rows`, the dock's Rows and Row panels, `#lib/plain`): paging, sorting where
Lance orders a column, text search only where a full-text index is, similar rows only where a vector index is, a blob
cell's image or audio previewed through a signed media URL, every read bounded (a page's depth, a sort's keys, a cell's
text and list cut at the read, a plain binary cell never read); nothing is guessed and nothing is built. Its Atlas, Graph
and Tree are the person's to set up (`viewer.services.derived_views`, `#lib/plain/SetupForm.svelte`): they pick the
columns (two numbers or a vector for the atlas, which the viewer projects by PCA on numpy's `eigh`; an edge table's from
and to, or an id and what it points to, for the graph; an id and a parent, or a path and its separator, for the tree),
the form opens on what the table's profile suggests and fills a role in only where exactly one candidate fits
(`#lib/plain/suggest.ts`), and nothing is drawn until they confirm; while they edit, the form asks only the
`graph/check` and `tree/check` doors, which read the profile's sample, once the columns have stood still. Each derived
read (`GET .../tables/{table}/profile`, `atlas`, `graph`, `tree`, their checks and node rows) is behind the same gate as
the rows, names columns only after checking them against the schema (a duplicate pick is refused), reads an evenly
spaced sample past its cap through Lance's `take` with every text cell cut in the projection, stops at its deadline and
at 96 MB of values (`READ_BYTES`) and says so, leaves out and counts a non-finite weight, size or position, answers
strict JSON, keys a graph node by its type (`i:1` and `s:1` are two nodes), splits a path to at most 16 levels, keeps
the heaviest edges and largest branches, is kept per (dataset, table, version, columns) within ONE byte budget shared
with the stamped atlas's points (`media.viewer.cacheBytes`), takes one of `media.viewer.derivedBuilds` build slots when
cold or is refused with 503 and Retry-After (`viewer.services.build_slots`, the clip transcode's mechanism), stops when
every caller has left, and writes nothing. A node picked in the graph or the tree lists in the Rows panel the rows its
count counted, from `graph/rows` or `tree/rows` by the node's key (a tree node: the rows at or below it; its
"(itself)" leaf: the rows at it alone). Set-ups apply only to a table with no descriptor: a stamped dataset's Atlas,
Graph and Tree are its descriptor's, and a link carrying a set-up to one says it was not used. It reaches a
lakehouse only through what rask publishes, the way any outside tool would:

- data through stock `lance_namespace` and pylance, on the credentials the catalog vends;
- every other call through the catalog's and lineage's doors (`docs/rask/*-openapi.json`);
- the lakehouse's events as `docs/rask/COMMIT-EVENTS.md` describes them.

**Its authentication and authorization are its own and have nothing to do with rask's.**
- **Sign-in** is any OIDC provider the cluster runs (owner, 2026-11-16: Dex included). Neither rask nor the workbench
  hosts identity. The workbench has its own public PKCE client and its own audience there. Token exchange and DPoP are
  used only where the provider offers them: the app binds its tokens to the tab with DPoP (RFC 9449) when the
  provider's discovery advertises `dpop_signing_alg_values_supported` (`frontend/app/src/lib/auth.svelte.ts`), and
  every service verifies the token, and its proof unless the deployment says the provider binds none
  (`WORKBENCH_DPOP_REQUIRED=false`; a token naming a key is never accepted without its proof), on every request
  (`service_kit.governed.deps`, `service_kit.governed.dpop`). A person is named exactly as rask names them, from issuer
  and subject (`<idp id>~<subject>`, Dex's connector map, several issuers, a client's token naming nobody:
  `service_kit.governed.identity`), so a grant in the workbench's store and one in rask name the same person.
- **Authorization** is the workbench's own OpenFGA model and store (`service_kit/governed/auth/model.fga`): an org's
  admins and members, a project's five roles (owner > manager > reviewer > annotator > viewer), and the tasks, corpora
  and flows that inherit from a project. It never asks rask's decision door or rask's OpenFGA store, and rask knows
  nothing of the workbench's users or roles. The model and the declared organisations are written by the model writer
  (`python -m service_kit.governed.auth.write_model`), a Job the deployment runs; a service finds the store by its name
  and the model its own image carries, and never writes either.
- **Data** in a rask lakehouse is rask's to govern. A read is made as the person: where the provider exchanges its own
  tokens (RFC 8693, Keycloak), on a token the service exchanges for them (`service_kit.governed.token_exchange`,
  `WORKBENCH_LAKEHOUSE_DELEGATION=exchange`); where it does not (Dex), the app's sign-in asks for the lakehouse's
  audience too, and the service passes the person's own token on unchanged, only when it names that audience and no key
  (`forward`). Delegation is per issuer, and a token is never sent to an issuer other than its own. A publish is written
  as the workbench's own confidential client after the workbench's own `can_publish` check on the project, into a
  namespace of the project's warehouse, and names the person in the commit; a corpus binding grants reading, never
  writing. rask decides whether that caller may, and its refusal is final.

The settings, one `WORKBENCH_*` name each (`service_kit.governed.settings`); every credential is a mounted file:

| Setting | What it names |
| --- | --- |
| `WORKBENCH_OIDC_ENABLED`, `_ISSUER`, `_AUDIENCE`, `_IDP_ID`, `_DISCOVERY_URL` | sign-in, on by default; the provider, the workbench's own audience, and the idp id its people are principals of (`<idp-id>~<subject>`) |
| `WORKBENCH_OIDC_SUBJECT_CLAIMS`, `_CONNECTORS`, `_CLIENT_MARKER`, `_PROVIDERS` | how people are named, as rask names them (rask's ADR 0208): the subject claims, a Dex connector map (`{"local": "dex"}`), the issuer's client marker, and further issuers |
| `WORKBENCH_DPOP_REQUIRED` | whether every token must come with a DPoP proof: on by default; off only for a provider that binds no token, and then a bearer is accepted only when it names no key |
| `WORKBENCH_INSECURE_ALLOW_UNAUTHENTICATED` | the only way to run with sign-in off, refused unless sign-in is off; a service with it logs a warning at start |
| `WORKBENCH_PUBLIC_URL`, `WORKBENCH_PUBLIC_MOUNT`, `WORKBENCH_DPOP_WINDOW_SECONDS` | the URL a DPoP proof's `htu` names (and the path a gateway rewrites it to), and the proof's `iat` and replay window |
| `WORKBENCH_OIDC_CLIENT_ID`, `WORKBENCH_OIDC_CLIENT_SECRET_FILE`, `WORKBENCH_LAKEHOUSE_AUDIENCE`, `_AUDIENCE_SCOPE` | the workbench's confidential client, which exchanges a person's token and takes its own, both addressed to the lakehouse (asking for the audience's scope where the provider reads one, Dex's `audience:server:client_id:<audience>`) |
| `WORKBENCH_LAKEHOUSE_DELEGATION` | `exchange` (default) or `forward`: how the primary issuer's people's reads reach the lakehouse; `forward` needs `WORKBENCH_DPOP_REQUIRED=false`. A further issuer names its own in its `WORKBENCH_OIDC_PROVIDERS` entry (`lakehouse`, with its own client), or its people's reads are refused: a token is exchanged only at the issuer that signed it. Under `forward` a read presents the person's latest token, never one past its own `exp` (`token_exchange.PersonTokens`) |
| `WORKBENCH_SOURCES_FILE` | the sources declaration (`{"sources": [...]}`, `client_kit.sources`), a file the chart renders from `sources` (`<release>-sources`); a relative local path in it is read beside the file. `WORKBENCH_SOURCES` gives the list inline (tests). No source names a credential: a property or storage option off the allow-list (`client_kit.source_keys`) is refused at load, by name |
| `ANNOTATOR_STATE_HOST`, `_PORT`, `_DATABASE`, `_USER`, `_SSLMODE`, `_CA_FILE`, `_PASSWORD_FILE` | the annotator's own PostgreSQL (its projects, tasks, drafts and claims): names only, the password a mounted file read on each new connection; `verify-full` against the CA by default, `disable` refused off the loopback; unset, the project and task plane answers 503 |
| `ANNOTATOR_PUBLISH_LEASE_SECONDS`, `_REDRIVE_SECONDS`, `_CONCURRENCY` | a publish run's lease on its project (default 300 s), how often each replica sweeps for publishes no live run holds (30 s, jittered), and how many runs one replica drives at once (2, below the pool size) |
| `WORKBENCH_FGA_ENABLED`, `_API_URL`, `_STORE`, `_MODEL_ID`, `_CREDENTIAL_FILE` | the workbench's own OpenFGA store, found by name, its model by the body the image carries unless pinned; required with sign-in on beside a source the workbench authorizes, and the chart gives it to the viewer and search there as to the annotator |
| `VIEWER_MEDIA_URL_KEY_FILE` | a credential: the mounted file holding the viewer's media-URL signing key, base64 of at least 32 random bytes (`openssl rand -base64 32`), read on every use; unset or short, the signing door answers 503 |
| `VIEWER_MEDIA_URL_TTL_SECONDS` | how long a signed media URL serves its cell: default 300, from 10 to 3600 |
| `WORKBENCH_FLOWS_SERVE_TOKEN_FILE` | a credential: the mounted projected ServiceAccount token (audience `serve`) flows presents to Ray Serve, read on every call; unset, every call to Serve is refused |
| `WORKBENCH_FLOWS_STATE_HOST`, `_PORT`, `_DATABASE`, `_USER`, `_SSLMODE`, `_CA_FILE`, `_PASSWORD_FILE` | flows' own PostgreSQL (each run and each step's outcome), named as the annotator's is; unset, the run doors answer 503 |
| `WORKBENCH_FLOWS_RUN_LEASE_SECONDS`, `_REDRIVE_SECONDS`, `_CONCURRENCY` | a run driver's lease on its run (default 30 s, renewed every third of it), how often each replica sweeps for runs no live driver holds (10 s, jittered), and how many runs one replica drives at once (4, below the pool size) |
| `WORKBENCH_FLOWS_RUN_MAX_ATTEMPTS`, `_RETENTION_DAYS` | how many times a run may be claimed before it fails for good (default 5), and how long an ended run stays before every replica's sweep deletes it (30 days) |

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
confirmed and the next step: **read #9's latest comment before starting.** The owner's order (2026-11-16) is to make
it run before any feature step: `make local` runs the whole app on demo data with sign-in off (R1), and the annotator
keeps its state in its own Postgres (R2). R3 made sign-in work with whatever OIDC provider the cluster runs (token
exchange and DPoP only where the provider's discovery offers them) and built the first cluster run (`make deploy-dev`,
docs/DEPLOY.md). R4 made the workbench read any Lance source (`client_kit.sources`, above); R5 made a dataset one dock whose panels are
every view (graph and tree beside the atlas, one navigation, the view in the address); R6 the set-ups of any table;
D3b-2a the labeling canvas's classes, names and review panel, and audio and video labeling on the shared viewers;
D3b-2b text spans, compare and assist; then work in progress moved into the annotator's Postgres (Lance written only
at publish); then D3c and D4. Flows' runs no longer need a sidecar: each run and each step's outcome are rows in flows'
own PostgreSQL, driven under a lease by any replica and resumed after a crash by any replica's re-drive
(`flows.store`, `flows.runner`), so the workbench runs without Dapr. Flows has no chart workload yet; the one that
deploys it comes with a CloudNativePG cluster of its own beside the annotator's. Of `frontend/annotator`, the app now holds its text viewer and
span surface (`TextViewer`, `TextSpanSurface`, `DocumentTextLane`, `span-render`, `span-remap`, `text-lane`, in
`#lib/canvas/text` and `#lib/canvas/span-remap.ts`), its interactive assist (`AiAssistBar`, the click session,
`assist.remote`, as `#lib/canvas/AssistPanel.svelte` and `#lib/api/assist.api.ts`) and a version compare in place of its
`compare.ts`/`CompareView` (`#lib/canvas/compare`); `AdjudicationPanel` was already in. Not ported, by decision: the
multi-key pairwise preference view (`CompareView` over `?keys=`; a task opens its first unit), the few-shot propagate
panel and its jobs door (`PropagatePanel`, `propagate`: batch derivers belong to flows, D4), and the zone-wide `/assist`
registry page (the canvas's panel lists the producers). Nothing
has run on a cluster yet. Two live copies of one session must never drive the same worktree: before resuming an agent
after a restart, check `~/.claude/sessions/*.json` for a second process with the same session id.
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
| Run it locally with real data | `make local`: installs, seeds three demo sources under `LOCAL_CATALOG` (`scripts/seed_demo_corpus.py`, declared by `scripts/local-sources.json`): a stamped page corpus (with an atlas, a topic tree and a small knowledge graph), an audio dataset and a video dataset (two short WebM films with sound, encoded by PyAV) in a directory catalog standing in for a lakehouse (`lake`), an unstamped table two namespaces deep in a lance-namespace directory catalog (`archive`) and a bare `.lance` table with an image blob (`scans`), each with columns a person can set an atlas, a graph and a tree up on, builds the app, and runs the viewer, search, annotator and flows with sign-in off behind the app's Gateway stand-in (`scripts/local-stack.sh`), on the loopback, printing the URL to open (http://127.0.0.1:8700/datasets; a port already in use is refused); the annotator's and flows' state run on the pinned PostgreSQL (`make postgres`) on :8705, a database each, its data kept beside the catalog (`<catalog>-state`), and the anonymous person administers the organisation `local`, so projects can be created and labelled. Ctrl-C stops it; GNU make itself then prints `*** Interrupt`, which `bash scripts/local-stack.sh` run on its own (after `make local` once) does not |
| Stop the local stack | `make local-stop`: stops the process `make local` recorded (`$TMPDIR/workbench-local-stack.pid`) and waits until its ports are free; `make local` checks the ports before it installs or builds anything |
| Deploy to the development cluster | `make deploy-dev` (images by commit, the OpenFGA fixture, the Dex client, the release, the lakehouse grants, a read-back), each step its own `make dev-*`; `make undeploy-dev` removes it all (docs/DEPLOY.md). The dev release also reads demo data of its own, the source `demo` (the corpus, recordings and films `make local` seeds), from a claim a Job fills once with `scripts/seed_demo_corpus.py --once` (`demo.enabled`, `chart/templates/demo.yaml`), read-only like every source but a lakehouse and marked `annotatable: true`, so its items are sent to a project and labelled there |
| The chart's renders | `make chart-check` (lint, the dev overlay, a platform's, a release with no lakehouse and one with sign-in off, and every misconfiguration the templates refuse, each rendered and failing with its own message: `scripts/chart-refusals.sh`) |
| Tests (no live stack) | `make test` (= `uv run pytest -m "not e2e" -n 8`) |
| One test | `uv run pytest services/<svc>/tests/<file>.py::<test>` |
| Lint, format, type-check | `make check` |
| The authorization model's tests | `make fga-test` (= `dagger call fga-test`); after editing `model.fga`, `make fga-model` |
| The pinned OpenFGA server for the suite | `make openfga` (exported through Dagger to `.localbin/openfga`) |
| The pinned PostgreSQL for the suite and the local stack | `make postgres` (the server tree exported through Dagger to `.localbin/postgres`, `scripts/postgres-server.sh`) |
| Frontend gates | `make frontend-check` (check, lint, fmt:check, vitest, knip); the app's Playwright suite `make app-e2e`, or `dagger call app-end-to-end` |
| The app with hot reload | `make app-dev` (vite on :5173, `/api` proxied to `WORKBENCH_EDGE_URL`, the local stack's edge on :8700 by default) |
| Build an image | `make image NAME=<stem>` (= `dagger call image --name=<stem>`) |
| The CI gates locally | `dagger call lint`, `dagger call test`, `dagger call frontend-check`, `dagger call app-end-to-end` |

## Repository layout

Two language-pure planes, as in rask; never blur them.

- `packages/`: reusable Python libraries, no entrypoints. `service-kit` (the service base: app factory, governed
  auth, settings) and `client-kit` (the media app factory and dataset registry the viewer, search and annotator
  share, and `client_kit.sources`, the declared sources). Every dataset is a namespace of a declared source opened
  through lance-namespace, as the caller on a lakehouse (on the credentials the catalog vends) and with no token anywhere
  else: the workbench holds no store key.
- `services/`: runnable Python. `viewer`, `search`, `annotator`, `flows`, each with its own `tests/`.
  - The annotator keeps its projects, tasks, drafts and claims in its own PostgreSQL (`annotator.state.store`, psycopg 3
    with its pool, plain SQL over `state/schema.sql`, applied at start under an advisory lock): each row holds its Pydantic
    document as JSONB beside the columns a query locks or filters on. A transition is one transaction that locks the
    project row, then the task row, and applies the pure machine (`annotator.projects.machines`); counts, the task index and
    `may_publish` are queries; a lease is a holder and an expiry, read as released once past it against the database
    clock, with no timer. A publish runs in-process under a lease on its project's row
    (`annotator.projects.lakehouse.PublishRunner`): started by the `publish` door, renewed between the saga's steps,
    and claimed by any replica's re-drive (about every 30 s) once its run stopped renewing; a run holds no connection
    between store calls, and a replica drives at most `ANNOTATOR_PUBLISH_CONCURRENCY` at once. Its tests run
    on a real PostgreSQL (`tests/postgres.py`, `state_database`; `services/annotator/tests/conftest.py`'s `state_app`).
  - The viewer answers a document's waveform (`GET /api/datasets/{id}/documents/{doc}/peaks`, `viewer.services.peaks`):
    its media blob streamed through PyAV (FFmpeg's demuxers and decoders as a library, no ffmpeg binary), resampled to
    8 kHz mono and reduced to each bucket's min and max as signed bytes, at most 360,000 buckets (100 a second up to an
    hour), bounded in bytes read, seconds decoded and time (`cut` says which stopped it), kept and built once like a
    derived view (the derived build slots and the shared cache budget). Measured on an hour: WAV 115 MB in 0.9 s, Opus
    36 MB in 4.4 s, under 75 MB of memory, a 950 KB answer.
  - The viewer serves media to `<img>` and `<video>`, which send no token, through short signed URLs
    (`viewer.api.v1.endpoints.signed_media`): `POST /api/signed-media` (DPoP) resolves each cell by the person's own
    read and signs it with the key in `VIEWER_MEDIA_URL_KEY_FILE`; `GET /api/signed-media/{token}` reads exactly that
    cell, at that version, as the viewer's own identity (the link is the permission), sandboxed and immutable.
- `runners/assist`: the annotator's assist model server, a sealed environment with its own `uv.lock`; not a workspace
  member, so its model pins never enter the services' resolution.
- `frontend/`: the bun workspace root (`package.json`, `bun.lock`, `knip.json`, oxlint/oxfmt config, `patches/`).
  - `frontend/app`: THE app (#9): one browser-only SvelteKit 3 single-page app (adapter-static, `ssr = false`, no
    server code), served by its own `serve.ts` with the strict CSP as a header; `edge.ts` beside it stands in for the
    Gateway, for the Playwright suite (its mock services) and the local stack (the real ones). It signs in with PKCE on the
    workbench's public client (oidc-client-ts), binds the tokens to the tab with DPoP (an in-memory, non-extractable
    P-256 key) where the provider's discovery offers it, renews a session with its refresh token, and calls each service same-origin at the public base `/config.json` names (`services`), which is also
    what every proof's `htu` names. Its data layer is `#lib/reads.svelte.ts`; its fetch seam is `#lib/api/client.ts`,
    which `@workbench/explorer-api` also calls through (`#lib/api/explorer.ts` installs it); every media element's
    link comes from one batched loader (`#lib/api/media-links.svelte.ts`); its explorer is `#lib/explore`; its viewers, shared by the explorer's player and the labeling canvas and importing neither, are `#lib/media` (the WebGPU image stage with fit, zoom and pan, whose pixi entry carries `pixi.js/unsafe-eval` for the CSP; a recording's transport, whose scrubber takes an overlay on its time axis; the synced transcript); its dock is
    `#lib/dock` (horizon-layout 1.0.5, `patches/horizon-layout@1.0.5.patch`). A dataset is ONE dock (`/datasets/[id]`), the
    page's only navigation: Results, Player, Atlas (`#lib/atlas`), Graph (`#lib/graph`) and Tree (`#lib/topics`) are its
    panels, each opened from the dock's "open a panel" menu, which lists a panel the dataset cannot feed disabled with its
    reason (`GatedAction`); the reasons are `panelGates` in `#lib/explore/pane-capabilities.ts`, and an open panel the
    dataset cannot feed shows the same reason. On a plain table the Atlas, Graph and Tree are `Plain*Panel.svelte`,
    offered as "Set up…" where the table's columns could feed them (`plainGates`) and holding their set-up in
    `#lib/plain/plain.svelte.ts`; a set-up naming a column the table no longer holds says so and offers to set up again. A
    dataset's layout is kept per dataset (`#lib/dock/store.ts`); one never arranged opens with the default that fits it
    (`defaultLayout`: only the panels it can feed, the views tabbed together). A dataset's named views (the Views rail,
    `#lib/dock/views.svelte.ts`) are the arrangement, a plain table's set-ups and the search or node its rows listed,
    several per dataset, one of which may be its default, which it opens with when no link says otherwise. The panels share one selection (`explore`, the atlas's `crossFilter`, `#lib/graph/graph-explorer.svelte.ts`):
    a topic chosen in the tree is the search's `topic=`, an entity picked in the graph searches for its name, and the
    Atlas lights up the results. The address is the view (`#lib/dock/link.ts`): the search, the topic, the row opened,
    the graph's entity, a plain table's set-ups and the node its rows are filtered to (validated, bounded, and left out
    with a notice when they are not readable, `#lib/plain/setup.ts`), the panels shown and the one maximized are written
    into it and restored from it. A maximized
    Atlas or Tree stands in for the page it replaced (the map beside its player over the selection's table; the treemap
    beside the topic's rows). The old `/datasets/[id]/atlas`, `graph` and `tree` links redirect to the dock with that
    panel in front. The atlas and the graph draw with WebGPU only (`#lib/gpu`), releasing their device when the panel
    unmounts; a browser without it gets their plain refusal and fetches no points. The annotator's project plane
    is `/projects` and `/projects/[project]` (`#lib/projects`, its calls `#lib/api/projects.api.ts`): a project's
    read answers the reader's `permissions`, and a page offers only what its door would let through; a dataset's
    search results and atlas selection are sent to a project from `#lib/projects/SendToProjectDialog.svelte`. A task's labeling canvas is `/projects/[project]/tasks/[task]` (`#lib/canvas`: the engine, folded in from `@workbench/engine`, drawing its annotation layer over the shared image stage; WebGPU only; its annotations, tags and versions at the annotator's `/api/annotations/<unit key>` with `?project=`, `?task=` and `?dataset=`, `#lib/api/annotations.api.ts`, where the door reads the unit against the task's own record (a task's work is its item's first unit, `access.work_unit`) and takes an edit only from the claim's holder or a reviewer of work in review, `annotator.annotations.access`; the rows are the task's draft in the annotator's Postgres, served as Arrow (`annotations.work.wire_table`, the draft's revision as `X-Annotations-Version`) and changed by a save of only what changed, links included, in one transaction under the task's row lock (`AnnotatorStore.change_draft`, 409 on a stale revision of that task, a change that changes nothing writes nothing), a draft held to 10,000 shapes, 10,000 links and 8 MiB whichever door changes it (413 naming the cap), each save kept in a history of the task's last 50 within 8 MiB (`MAX_DRAFT_HISTORY`, `MAX_HISTORY_BYTES`, `draft_history`: each row what undoes its save, a past save rebuilt from the draft by undoing the later ones) that the History panel lists and reads a past save from; so a consensus item's replicas never share rows; a replica's rows and draft are read only by its own annotator or someone who may review it, `tasks.require_work_readable`, and a person who ever held one replica of a group may never claim or be assigned a sibling, `Task.holders`, so a release-and-claim hop cannot reach another annotator's work). A text item (a dataset with no document, sent as `text`) is its row's own text (`text/TextCanvas.svelte`); under a picture or a recording every row with text is a line (`text/TextLane.svelte`); a selection is a span (a `text` row naming its parent and `char_start`/`char_end`, offsets in code points on the wire and in the canvas, `text/codepoints.ts`, saved on the `spans` channel) with the class picker, overlapping spans on lanes, edges dragged or moved with Alt+←/→, and a corrected line (Enter applies it, Ctrl+S applies and saves) re-anchors its spans by a character diff (`span-remap.ts`, jsdiff), dropping a span whose words are all gone; the item's own text is written as a row (`source: dataset`) when the first span is marked in it, and Submit names an unclassed span as it names a region (`validate_against_ontology`). Compare (`compare/ComparePanel.svelte`, `compare/compare.ts`) sets, for someone who may review the item, another replica's work (its task's own draft in the annotator's Postgres) or the model's proposals beside the item's own, pairs them by overlap, draws theirs (a second Arrow layer on the picture, lanes on the time strip, dashed under a text) and takes one in, on the undo stack. Assist (`AssistPanel.svelte`) asks the annotator's `POST /api/assist/<unit key>?project=&dataset=` (the task's id in the body, the unit one of that task's; a model server fetches the frame at `client_kit.media.frames.chunk_frame_path`) with a detector's prompt or a segmenter's clicks; proposals land as `prediction` rows to keep (A) or reject (R). A drawn region opens the class picker (`ClassPicker.svelte`; the task's ontology classes, never the labels already on the item; digits, type-ahead, Enter, the last class the default; Escape discards a region on a task whose classes are closed; the open picker takes keys wherever the focus is, `controller.pickerKey`); the digits relabel the selection. The side panel's head (`TaskPanel.svelte`, `task-panel.ts`) says what the person is doing from the task's state, holder and their permissions, offers the transitions its `legal_events` allow (the task door answers them), lists what blocks a submit (`classes.ts`, the same rule as `validate_against_ontology`: every region carries a class once the ontology names any), and carries a reviewer's send-back note to the annotator. The page names the item from its row and dataset declaration (`#lib/projects/item-name.ts`) and names the breadcrumb's project and item (`#lib/shell/crumb-names.svelte.ts`), the ids as tooltips. A recording's or a video's unit opens the whole document on one time line (`temporal/TemporalCanvas.svelte`): the video's frame drawn by the image stage from a hidden `<video>` (a pixi `ImageSource` over the element, which the stage never empties as a `VideoSource`'s destroy would, frames uploaded per `requestVideoFrameCallback`), under it ONE time strip (`#lib/media/time/TimeStrip.svelte`, hand-written WebGPU rectangles in `#lib/gpu/rects.ts`: the waveform, speakers, parts, region lanes and each track's lane of keyframes), then the shared transport. Regions are `segment` rows in seconds (`t_start`/`t_end`, the save's `temporal` channel, the one undo stack); a video's tracks are box rows sharing a `group`, each keyframe its `t_start`, interpolated between, absent from an outside keyframe (a box of no area) to the next (`temporal/tracks.ts`). The dataset's player draws the same strip, read-only, and a video's frames through the same image stage. Image
    `.docker/frontend.dockerfile`; released by the chart (`chart/`, `app.*`) at `/`.
  - `frontend/explorer`: only its `/workflow` editor and what that needs (its layout, `lib/workflow`, the atlas map
    its Atlas node opens, the player), until D4 merges it with studio's flows. `frontend/annotator`, `frontend/studio`:
    the old SvelteKit 2 apps, moving into `frontend/app` (#9 D3, D4); never deployable. The annotator's project pages
    and its image canvas are in the app (D3a, D3b-1); its temporal and text viewers, compare and assist follow (D3b-2), then its data manager and browse (D3c), which deletes it and `@workbench/engine`, kept until then for it alone.
  - `frontend/packages/*`: their libraries, `@workbench/*` (`ui`, `api`, `config`, `flow`, `explorer-api`,
    `engine`, `labeling`). `@workbench/ui` is read from source: its exports name `src/`, with no build step.
- `.docker/<stem>.dockerfile`: one per deployable (`media` = viewer + search, `annotator`, `flows`, `assist-runner`, `frontend` = the app),
  built from the repository root against the root `uv.lock`.
- `.dagger/`: the Dagger module (`image`, `lint`, `test`, `frontend-check`, `app-end-to-end`, `fga-test`).
- `chart/`: the workbench's release (the annotator, the viewer and search from the `media` image, the app, the
  annotator's Postgres and the model writer), every object named from the release; it reads the `sources` its values
  declare (`<release>-sources`, validated at render): a rask lakehouse by reference, any other source by URI or on a
  claim (at a `path` on it), so it runs with no rask at all. Development only, `demo` renders the claim and the Job
  (the media image, which carries the seeder) that fills it once for the demo source.
- `deploy/`: the development estate's overlay (`values-dev.yaml`), the clients it supplies to rask's dev Dex
  (`dex-client.yaml`), and `fixtures/openfga`, the workbench's own development OpenFGA. `docs/DEPLOY.md` says how a
  release is installed on the development cluster (`make deploy-dev`) and on a platform.
- `docs/rask/`: rask's published contract, vendored (`PROVENANCE.md` says from which sha and how to refresh it).
- `scripts/`: dev and ops scripts.

Workspace membership is globbed: `[tool.uv.workspace] members = ["packages/*", "services/*"]` and
`frontend/package.json` `workspaces = ["app", "explorer", "annotator", "studio", "packages/*"]`. A new Python member must ship a
`pyproject.toml` (uv refuses the workspace otherwise); a new JS package must ship a `package.json` (bun silently skips
it otherwise). Add a new Python import name to ruff's `known-first-party` in the same change.

## Git

- Commit messages are Conventional Commits (the commit-msg hook checks it) and carry no `Co-Authored-By: Claude` line
  (the hook refuses it).
