# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**The workbench: explore, search and annotate the datasets in a rask lakehouse, read any Lance data beside it, and run
model pipelines over them**, as one product (FiftyOne-like). Its parts:

- **explorer**: the viewer, search, and a dataset's views in the app;
- **annotator**: the annotator and its UI, with assist asking the deployment's model endpoint;
- **flows**: model pipelines; parked, with no screen and no deployment (owner, 2026-11-18; ADR 0011), its old editors
  deleted (ADR 0021).

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
  `service_kit.governed.identity`), so a grant in the workbench's store and one in rask name the same person. How long a
  signing key is trusted is one rule in every service (`service_kit.governed.oidc.OIDCVerifier`): one background
  refresher per process (started by the app's startup, stopped at shutdown, restarted by a request if it ever ended)
  reads each issuer's key set every 60 s and discovery every cache TTL, gives each attempt 5 s in all (each read is
  streamed raw with `Accept-Encoding: identity`, cut at 1 MiB, follows no redirect, and is cut at that deadline from
  outside by a watchdog that shuts its connection down, so its thread ends then when it was in the TLS handshake, the
  headers or the body; the connect is bounded per address the name resolves to, at most what was left of the
  deadline for each, and a name lookup by nothing here; an attempt past it is abandoned and the next
  starts on a new connection, at most 2 abandoned ones running per issuer), and tries a failed one again after 5 s; a
  request never reads and never waits, and judges its token on the set read last while that is younger than 300 s,
  else answers 503 at once with Retry-After. So a key the issuer drops (Dex on memory storage drops them all at a
  restart) is refused everywhere within about a minute, and none is trusted past 300 s from its read while the issuer
  is away. A key that was in a set the process read and is absent from the two successful reads after it (periodic or
  forced, `RETIRE_AFTER_READS`; one read is not enough, since replicas of an issuer can serve different sets) is
  retired: a token naming it is refused 401 at once, with no read, no wait and no pace spent. Each issuer's own; only
  keys the issuer published; never one the current set holds again; remembered 24 h and at most 32 per issuer
  (`RETIRED_KID_SECONDS`, `RETIRED_KIDS_PER_ISSUER`); an issuer that serves a set without a key it still signs with for
  two reads running has that key refused until a read holds it again. A key absent from one read so far, a forgotten
  one, or one a pod that started after the rotation never saw, meets the rule that follows. The one wait: a token
  naming a key the set does not hold and that is not retired asks for one forced read (at most one per issuer per
  30 s), and it and every such token arriving while that read is in flight, at most 8 per issuer (`FORCED_WAITERS`:
  a page's parallel reads after signing in again at a restarted issuer fit, a flood of forged `kid`s parks no more),
  wait for its outcome at most 5 s, and is let in when that read holds its key. Such a token is refused 401 ONLY when a
  read that STARTED after it asked found a set without its key (a read that began before it may predate the key);
  past the cap, with the pace spent, when the read failed, or when the read it joined began before it and lacks the
  key, it is a 503 with Retry-After (the
  sooner of the pace, the periodic read and the retry of a failed forced read, at most 30 s), so a forged `kid` ends
  nobody's session. The app sends a read that met such a 503 again after its Retry-After (`#lib/api/client.ts`, at
  most 3 times and 30 s in all; never a write), and answers any 401 that challenges the session (`WWW-Authenticate`:
  the door's own, and the delegation's "the session must be renewed") with ONE prompt to sign in again that comes back
  to the same address (`#lib/reauth.svelte.ts`, the shell's session prompt), the page behind it keeping what its reads
  had answered (`#lib/reads.svelte.ts`: a read the session was refused for leaves an answer it held), never a 403, a
  503 or a 401 a service only relays, and never sends the person away on its own. After a restart of the issuer, the
  first read a service refuses asks it once more (the second read without the key) and is refused; the page's reads
  that wait on that same read are refused when it ends, and every read after it at once.
- **A reload keeps the session through the issuer's own** (ADR 0020; `#lib/auth.svelte.ts`), where the deployment
  allows it (`/config.json`'s `silentSignIn`, the chart's `app.oidc.silentSignIn`, on by default; off for an issuer
  with no session of its own, such as the dev Dex v2.46.1 without `DEX_SESSIONS_ENABLED`, which shows its form for
  `prompt=none` and with its approval screen then refuses it; off changes only that, and the sign-out record and the
  back/forward cache reload below hold either way). Tokens stay in memory; the first sign-in a tab sends asks with
  `prompt=none` by a top-level redirect (no iframe: the issuer's cookie rides a top-level navigation under any
  third-party-cookie policy), so an issuer holding the person's login session signs them in again with no form, back at
  the same address (path, query and fragment, through the OIDC state). An issuer that answers an error is asked ONCE
  more, interactively, and never silently again until a sign-in completes (`workbench.silent-sign-in-tried` in
  sessionStorage); a page restored from the back/forward cache while its sign-in was in flight loads again. Sign-out
  ends the issuer's session at its `end_session_endpoint` (with `id_token_hint`, back to `<origin>/`, which the client
  must register as a post-logout redirect), and writes a new random id to `workbench.signed-out` in localStorage,
  which is never removed; a completed sign-in writes the id it answered to `workbench.signed-in-after`. While the two
  differ the person is signed out: no sign-in is silent and each sends `prompt=login`. Another tab still signed in drops
  its tokens when it sees a new sign-out id and raises the shell's one prompt (nothing navigates until the person
  chooses "Sign in again", which keeps the canvas's unsaved edits), and a sign-in sent before the last sign-out is
  discarded at its callback, whatever happened in between. None of these values is a credential.
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
| `WORKBENCH_DEFAULT_DATASET`, `WORKBENCH_DESCRIPTOR_DIR` | the dataset a request naming none reads (`<source>~<namespace>`; the annotator's must be annotatable), and where a dataset's descriptor may be given as a file |
| `WORKBENCH_CORS_ORIGINS`, `WORKBENCH_DOCS`, `WORKBENCH_MAX_BODY_BYTES`, `WORKBENCH_AUDIT_ENABLED` | what the app factory (`service_kit.app.make_service_app`) builds every service with: the origins besides the app's own a browser may call from (none by default), the interactive docs (off), the request-body cap (64 MiB; the viewer and search 32, the annotator 16), the audit trail (on) |
| `WORKBENCH_OTEL_ENABLED`, `WORKBENCH_OTEL_TOKEN_FILE` | whether a service exports its traces, metrics and logs (the audit trail on its own scope, `workbench.audit`, which the platform's gateway may route to a table of its own) over OTLP/HTTP to the `OTEL_EXPORTER_OTLP_*` endpoint, as its own `service.name`; and the mounted projected token each export presents, read when it is sent (`service_kit.otel`) |
| `ANNOTATOR_MODEL_ENDPOINT_URL`, `_TOKEN_FILE`, `_TIMEOUT_SECONDS` | the model endpoint assist asks by the workbench's model protocol (`docs/model-protocol.md`, ADR 0023 and 0030; the chart's `models.endpoint`), the mounted file holding the token the annotator presents to it as itself (a credential: a projected ServiceAccount token for the endpoint's audience, read on every call, required with a URL), and how long an ask may take (default 30 s); no URL, no assist, said to the person |
| `WORKBENCH_EMBED_URL`, `WORKBENCH_RERANK_URL` | the embedding and reranking model servers search queries and the viewer's health reports on |
| `VIEWER_CACHE_BYTES`, `VIEWER_DERIVED_BUILDS` | the one byte budget the viewer's drawn answers share (the chart's `media.viewer.cacheBytes`), and how many derived views it builds at once |
| `SEARCH_CACHE_SIZE`, `SEARCH_CACHE_BYTES` | search's version-keyed result cache: how many result sets, and how many bytes (0 disables each) |
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
names, changed freely: service-kit, and the frontend packages `@workbench/api` and `@workbench/config`. Its
licence is Apache-2.0 (`LICENSE`, ADR 0024). Its design system is `@riksarkivet/ui` (`AI-Riksarkivet/ui`), a repository of its own. Its settings are `WORKBENCH_*` environment variables
(the `LANCE_*` ones name what it asks of a lakehouse). rask's repository is `AI-Riksarkivet/rask`;
compute (Ray Serve) is a repository of its own, called over HTTP, and a model is an endpoint the workbench is given and never deploys (ADR 0023).

The code came out of rask's history at `64ef118a9` on 2026-11-14 (issue #1).

## Where the work stands

Work is tracked one GitHub issue per row, in the order of a pinned map issue (§ Agent skills). Until the owner's
`/to-spec` and `/to-tickets` file the first rows, the open issues are #1 and #6 to #10 (WB-A to WB-E); their bodies and
comments record what landed and the owner's decisions, #9's above all. The open work found in them, re-checked against
the code, is `docs/planning/INPUT-2026-11-20.md`, and the decisions already made are `docs/adr/`. The owner's order
(2026-11-16) was to make it run before any feature step: `make local` runs the whole app on demo data with sign-in off (R1), and the annotator
keeps its state in its own Postgres (R2). R3 made sign-in work with whatever OIDC provider the cluster runs (token
exchange and DPoP only where the provider's discovery offers them) and built the first cluster run (`make deploy-dev`,
docs/DEPLOY.md). R4 made the workbench read any Lance source (`client_kit.sources`, above); R5 made a dataset one dock whose panels are
every view (graph and tree beside the atlas, one navigation, the view in the address); R6 the set-ups of any table;
D3b-2a the labeling canvas's classes, names and review panel, and audio and video labeling on the shared viewers;
D3b-2b text spans, compare and assist; then work in progress moved into the annotator's Postgres (Lance written only
at publish); D3c-a the project's screens (data manager, browse, review, settings, people by name); D3c-b the bulk-labeling grid (the Label tab). Still open: live updates and deleting `frontend/annotator` (D3c-c); D4, one flows area, waits while flows is parked (ADR 0011). Each flows run and each step's outcome are rows in flows'
own PostgreSQL, driven under a lease by any replica and resumed after a crash by any replica's re-drive
(`flows.store`, `flows.runner`), so the workbench runs without Dapr. Flows has no chart workload yet; the one that
deploys it comes with a CloudNativePG cluster of its own beside the annotator's. Of `frontend/annotator`, the app now holds its text viewer and
span surface (`TextViewer`, `TextSpanSurface`, `DocumentTextLane`, `span-render`, `span-remap`, `text-lane`, in
`#lib/canvas/text` and `#lib/canvas/span-remap.ts`), its interactive assist (`AiAssistBar`, the click session,
`assist.remote`, as `#lib/canvas/AssistPanel.svelte` and `#lib/api/assist.api.ts`) and a version compare in place of its
`compare.ts`/`CompareView` (`#lib/canvas/compare`); `AdjudicationPanel` was already in. Not ported, by decision: the
multi-key pairwise preference view (`CompareView` over `?keys=`; a task opens its first unit), the few-shot propagate
panel and its jobs door (`PropagatePanel`, `propagate`: batch derivers belong to flows, D4), and the zone-wide `/assist`
registry page (the canvas's panel lists the models). Its bulk grid is the Label tab; not ported from it, by decision: its
model-filled recipe columns (a batch deriver, flows' D4), its similarity anchor (the dataset's own similar search finds
neighbours to send) and its inline transcription edit (the canvas's). The release
runs on the development cluster (`make deploy-dev`, docs/DEPLOY.md), and each landed step's comment on #9 names the
revision it was read back on. Two live copies of one session must never drive the same worktree: before resuming an agent
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
- **A row lands as one reviewed commit on `main`** (coherent rows together; `docs/agents/issue-tracker.md`). Two
  reviewers read it in parallel, one for standards and security and one for the spec and usability (a browser walk
  with `playwright-cli` for UI), and every finding in the row's own code is fixed, LOWs included. Then the gates
  (`dagger call frontend-check`, `dagger call app-end-to-end` at 0 failed and 0 flaky, the full `make test`), one
  commit with the tests delta and `Refs #n`, a push, a deploy to the dev cluster, a live read-back, and the row closed
  by hand naming the helm revision.

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
| Deploy to the development cluster | `make deploy-dev` (images by commit, the OpenFGA fixture, the Dex client, the release, the lakehouse grants, a read-back), each step its own `make dev-*`; `make undeploy-dev` removes it all (docs/DEPLOY.md). The dev release also reads demo data of its own, the source `demo` (the corpus, recordings and films `make local` seeds), from a claim a Job fills with `scripts/seed_demo_corpus.py --once`, which writes each demo dataset the claim lacks and keeps the rest (a marker per dataset under `.seeded.d/`) (`demo.enabled`, `chart/templates/demo.yaml`), read-only like every source but a lakehouse and marked `annotatable: true`, so its items are sent to a project and labelled there |
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

- `packages/`: reusable Python libraries, no entrypoints. `service-kit` (the service base: THE app factory every
  service is built by, `make_service_app`, with its exception handlers, middleware, probes, drain and telemetry;
  governed auth; settings) and `client-kit` (what the viewer, search and annotator compose onto that factory: their
  shared settings, lifespan, readiness and dataset registry, `client_kit.media`; and `client_kit.sources`, the
  declared sources). Every dataset is a namespace of a declared source opened
  through lance-namespace, as the caller on a lakehouse (on the credentials the catalog vends) and with no token anywhere
  else: the workbench holds no store key.
- `services/`: runnable Python. `viewer`, `search`, `annotator`, `flows`, each with its own `tests/`.
  - The annotator keeps its projects, tasks, drafts and claims in its own PostgreSQL (`annotator.state.store`, psycopg 3
    with its pool, plain SQL over `state/schema.sql`, applied at start under an advisory lock): each row holds its Pydantic
    document as JSONB beside the columns a query locks or filters on. Those columns are stored generated columns, and a
    stored value is never stale by construction: each is declared through `annotator_declare_generated`, which drops and
    adds it again (computing it for every row) when the expression the database holds is not the declared one, and every
    function a column calls is named by its body (`annotator_declare_function`: `<base>_<12 hex of the SHA-256 of the body
    with its whitespace normalised>`, which a column's expression names as `{{<base>}}`). So a function's body is edited
    freely: a cosmetic edit keeps its name and changes nothing, a real one gives a new name whose columns converge (every
    row computed once), and the functions no column calls any more are dropped in the same apply. A transition is one transaction that locks the
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
- `frontend/`: the bun workspace root (`package.json`, `bun.lock`, `knip.json`, oxlint/oxfmt config, `patches/`).
  - `frontend/app`: THE app (#9): one browser-only SvelteKit 3 single-page app (adapter-static, `ssr = false`, no
    server code), served by its own `serve.ts` with the strict CSP as a header; `edge.ts` beside it stands in for the
    Gateway, for the Playwright suite (its mock services) and the local stack (the real ones). It signs in with PKCE on the
    workbench's public client (oidc-client-ts), binds the tokens to the tab with DPoP (an in-memory, non-extractable
    P-256 key) where the provider's discovery offers it, renews a session with its refresh token, and calls each service same-origin at the public base `/config.json` names (`services`), which is also
    what every proof's `htu` names. Its data layer is `#lib/reads.svelte.ts`; its fetch seam is `#lib/api/client.ts`,
    which `@workbench/explorer-api` also calls through (`#lib/api/explorer.ts` installs it); every media element's
    link comes from one batched loader (`#lib/api/media-links.svelte.ts`); its explorer is `#lib/explore`; its viewers, shared by the explorer's player and the labeling canvas and importing neither, are `#lib/media` (the WebGPU image stage with fit, zoom and pan, whose pixi entry carries `pixi.js/unsafe-eval` for the CSP; a recording's transport, whose scrubber takes an overlay on its time axis; the synced transcript); its dock is
    `#lib/dock` (horizon-layout 2.0.2, unpatched: its tabs carry the WAI-ARIA semantics and a popout window mirrors the page's stylesheets and `<html>` theme itself). A dataset is ONE dock (`/datasets/[id]`), the
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
    is `/projects` and `/projects/[project]` (`#lib/projects`, its calls `#lib/api/projects.api.ts`). A project's page is
    six tabs, the tab and each tab's filters in the address (`?tab=`, `#lib/projects/item-query.ts`): Items (the data
    manager, `DataManager.svelte`: the annotator filters by state, holder, submitter, reviewer, class, dataset and date,
    sorts and pages a project's items in Postgres, `GET /projects/{id}/tasks` with `annotator.state.task_query` over
    columns `schema.sql` generates from each task's document, answering the page, how many match, each state's count and
    each person's part; the bulk actions fire one gated event per item), Label (the bulk-labeling grid, `LabelGrid.svelte`,
    `label-grid.ts`, `label-run.ts`: the items open to the reader's labeling, `open_to`, or for a reviewer the work
    waiting for them, each with its work, `include=work`, its draft's revision and whole-item tags, each saying whether it
    carries more than its class or is a model's proposal, from a generated `drafts.work_tags` column; the classes drawn
    as a tag are its labels, digits 1-9, every other class labelled on the canvas; a `role="grid"` keyboard grid, a label
    written to each item through the canvas's own save door on the revision read, claimed first where its `legal_events`
    offer it (and released again when the label is then refused), a few in flight, told as "n of N" with a Stop, each
    refusal named on its item and grouped by reason; a label that would discard another label's text, attributes, links
    or a model's proposal asks once, skipping those by default, and choosing a proposed class keeps the proposal; undo
    and redo are the annotator's own undo of each item's save, `POST /api/annotations/<unit>/revert`, from its
    `draft_history`, while the work is still at that revision; submit, accept and send back many, per item), Browse and Review (lists that open the canvas on
    a pass, `?pass=browse` read-only or `?pass=review` with Accept, Accept with my fixes and Send back on the side panel
    and Shift+A, Shift+F, Shift+R, Page Up/Down stepping the walk the annotator reads around the open item), Settings
    (`PATCH /projects/{id}`, the rules of what may still change said beside each) and Publish. People are shown by the
    name the annotator kept from their own verified token when they last signed in (the app's `/me` registers it) or
    wrote through one of its doors (`annotator.people`, the `people` beside the principals a read names,
    `PersonName.svelte`), never one a request states; the principal is the tooltip, and someone who never signed in is
    shown by it, said so; a manager grants a role by picking from the named people of the projects they manage in the
    organisation (`members/candidates`), or by a typed principal. A project's
    read answers the reader's `permissions`, and each task's `legal_events` are what THIS reader may fire on it now
    (their role and the identity rules, `machines.caller_events`), the one rule the table, the quick view and the
    canvas's panel offer from; a dataset whose items are its rows (a corpus of texts, no document) is browsed by its rows
    (`GET /api/rows`) and sent from there; a dataset's
    search results and atlas selection are sent to a project from `#lib/projects/SendToProjectDialog.svelte`. A task's labeling canvas is `/projects/[project]/tasks/[task]` (`#lib/canvas`: the engine, folded in from `@workbench/engine`, drawing its annotation layer over the shared image stage; WebGPU only; its annotations, tags and versions at the annotator's `/api/annotations/<unit key>` with `?project=`, `?task=` and `?dataset=`, `#lib/api/annotations.api.ts`, where the door reads the unit against the task's own record (a task's work is its item's first unit, `access.work_unit`) and takes an edit only from the claim's holder or a reviewer of work in review, `annotator.annotations.access`; the rows are the task's draft in the annotator's Postgres, served as Arrow (`annotations.work.wire_table`, the draft's revision as `X-Annotations-Version`) and changed by a save of only what changed, links included, in one transaction under the task's row lock (`AnnotatorStore.change_draft`, 409 on a stale revision of that task, a change that changes nothing writes nothing), a draft held to 10,000 shapes, 10,000 links and 8 MiB whichever door changes it (413 naming the cap), each save kept in a history of the task's last 50 within 8 MiB (`MAX_DRAFT_HISTORY`, `MAX_HISTORY_BYTES`, `draft_history`: each row what undoes its save, a past save rebuilt from the draft by undoing the later ones), of which the History panel lists the latest 20 and reads a past save from; so a consensus item's replicas never share rows; a replica's rows and draft are read only by its own annotator or someone who may review it, `tasks.require_work_readable`, and a person who ever held one replica of a group may never claim or be assigned a sibling, `Task.holders`, so a release-and-claim hop cannot reach another annotator's work). A text item (a dataset with no document, sent as `text`) is its row's own text (`text/TextCanvas.svelte`); under a picture or a recording every row with text is a line (`text/TextLane.svelte`); a selection is a span (a `text` row naming its parent and `char_start`/`char_end`, offsets in code points on the wire and in the canvas, `text/codepoints.ts`, saved on the `spans` channel) with the class picker, overlapping spans on lanes, edges dragged or moved with Alt+←/→, and a corrected line (Enter applies it, Ctrl+S applies and saves) re-anchors its spans by a character diff (`span-remap.ts`, jsdiff), dropping a span whose words are all gone; the item's own text is written as a row (`source: dataset`) when the first span is marked in it, and Submit names an unclassed span as it names a region (`validate_against_ontology`). Compare (`compare/ComparePanel.svelte`, `compare/compare.ts`) sets, for someone who may review the item, another replica's work (its task's own draft in the annotator's Postgres) or the model's proposals beside the item's own, pairs them by overlap, draws theirs (a second Arrow layer on the picture, lanes on the time strip, dashed under a text) and takes one in, on the undo stack. Assist (`AssistPanel.svelte`) lists the models of the deployment's model endpoint (`GET /api/assist/models`, which says assist is unavailable where the deployment names none) and asks one through the annotator's `POST /api/assist/<unit key>?project=&dataset=` (the task's id in the body, the unit one of that task's) with the prompts that model takes (words, a drawn box, clicks); the annotator sends the endpoint the unit's picture (`client_kit.media.frames.frame_row`, the frame the canvas draws) by the workbench's model protocol (`annotator.model_endpoint`, `docs/model-protocol.md`) as itself, and an endpoint that is slow or fails is an error the person reads, never an empty answer. Proposals land as `prediction` rows that name their model, its version and its score (a save lacking one is refused), to keep (A) or reject (R), or all at once; an item holding one nobody kept or removed is refused at submit, and at a reviewer's accept, with the count (ADR 0019, `machines.SETTLES_PROPOSALS`), and `POST /api/annotations/<unit key>/proposals` keeps or removes them all as one revision, which the Label grid offers per item from its `work.proposals` count. A drawn region opens the class picker (`ClassPicker.svelte`; the task's ontology classes, never the labels already on the item; digits, type-ahead, Enter, the last class the default; Escape discards a region on a task whose classes are closed; the open picker takes keys wherever the focus is, `controller.pickerKey`); the digits relabel the selection. The side panel's head (`TaskPanel.svelte`, `task-panel.ts`) says what the person is doing from the task's state, holder and their permissions, offers the transitions its `legal_events` allow (the task door answers them), lists what blocks a submit (`classes.ts`, the same rule as `validate_against_ontology`: every region carries a class once the ontology names any), and carries a reviewer's send-back note to the annotator. The page names the item from its row and dataset declaration (`#lib/projects/item-name.ts`) and names the breadcrumb's project and item (`#lib/shell/crumb-names.svelte.ts`), the ids as tooltips. A recording's or a video's unit opens the whole document on one time line (`temporal/TemporalCanvas.svelte`): the video's frame drawn by the image stage from a hidden `<video>` (a pixi `ImageSource` over the element, which the stage never empties as a `VideoSource`'s destroy would, frames uploaded per `requestVideoFrameCallback`), under it ONE time strip (`#lib/media/time/TimeStrip.svelte`, hand-written WebGPU rectangles in `#lib/gpu/rects.ts`: the waveform, speakers, parts, region lanes and each track's lane of keyframes), then the shared transport. Regions are `segment` rows in seconds (`t_start`/`t_end`, the save's `temporal` channel, the one undo stack); a video's tracks are box rows sharing a `group`, each keyframe its `t_start`, interpolated between, absent from an outside keyframe (a box of no area) to the next (`temporal/tracks.ts`). The dataset's player draws the same strip, read-only, and a video's frames through the same image stage. Image
    `.docker/frontend.dockerfile`; released by the chart (`chart/`, `app.*`) at `/`.
    Its design system is `@riksarkivet/ui`, the public repository `AI-Riksarkivet/ui`, read from source (no build
    step) and pinned by a full commit sha in `frontend/app/package.json` (`github:AI-Riksarkivet/ui#<sha>`; `bun.lock`
    records it, and the image's `bun install --frozen-lockfile` fetches that commit's tarball from GitHub). Its
    `docs/CONSUMING.md` is the contract, `DESIGN.md` the look; components, tokens, themes and THE Storybook live and are
    changed there, never here. Upgrading is bumping the sha and `bun --cwd=frontend install`, after reading the commits in
    between. The app owns the one copy of each peer (`svelte`, `bits-ui` with the workspace's patch, `tailwindcss`,
    `vite`, `gsap`). `app.css` imports `@riksarkivet/ui/styles.css` (fonts self-hosted from the app's own origin,
    `tw-animate-css`, the themes and tokens, and the package's `@source`) and the dock's theme
    `@riksarkivet/ui/styles/horizon-layout.css` after horizon-layout's own sheet, both under `layer(base)`; what stays in
    `app.css` is the app's own: the search-match `--highlight` and the popover surface rule. Colour is the person's: a
    shipped theme (`data-theme` on `<html>`) or an accent of their own corrected for WCAG AA, picked in the account menu.
    Where the choice is kept and what a first visit gets are named once, `#lib/appearance-options.ts` (key
    `workbench-appearance`, the `ink` theme, dark), passed to the library's Vite plugin `riksarkivetUi` (`vite.config.ts`),
    which emits `/appearance-boot.js` that `app.html` loads before the first paint (same-origin, so the CSP needs no
    hash), and to `appearance.start()` in the root layout; every canvas redraws on `appearance.resolved`. The theme CSS
    needs relative colour syntax (`oklch(from …)`): Chromium 119, Firefox 128, Safari 16.4 at least. What the library
    leaves to its apps is the app's own code: its shell (`#lib/shell`) and `Subject` (`#lib/identity`, a principal shown
    shortened with the full value as its tooltip).
    The supported browsers are current Chrome and Edge on a machine with a GPU (ADR 0018). A browser that cannot run
    the app is shown one plain page instead (`#lib/browser-support.ts`): `/browser-check.js` (built from
    `#lib/browser-check.ts` by `vite.config.ts`, loaded by `app.html` after the appearance boot script) stops the
    document's loading where relative colour syntax or `navigator.gpu` is missing, and the `init` hook
    (`src/hooks.client.ts`) waits up to `ADAPTER_WAIT_MS` for the GPU adapter, through the probe every canvas reads, and
    shows the page when none comes; an answer later than that starts the app, and each canvas refuses plainly. The
    browser suite runs the app in Chromium with its software adapter (`chromium`, `webgpu`), and the page in Chromium
    without an adapter (`no-webgpu`) and in Firefox (`firefox`), `e2e/unsupported.spec.ts`.
  - `frontend/annotator`: the old SvelteKit 2 app; never deployable. Everything of it that is kept is in the app (its
    project pages, every canvas, compare, assist, the data manager, browse and the bulk grid); what remains is deleting
    it and `@workbench/engine` and `@workbench/api`, kept until then for it alone (D3c-c, WB-109). It draws with
    `@riksarkivet/ui` at the app's pin and carries its own copy of the old shell (`src/lib/shell`).
  - `frontend/packages/*`: their libraries, `@workbench/*` (`api`, `config`, `explorer-api`, `engine`, `labeling`).
- `.docker/<stem>.dockerfile`: one per deployable (`media` = viewer + search, `annotator`, `flows`, `frontend` = the app),
  built from the repository root against the root `uv.lock`.
- `.dagger/`: the Dagger module (`image`, `lint`, `test`, `frontend-check`, `app-end-to-end`, `fga-test`).
- `chart/`: the workbench's release (the annotator, the viewer and search from the `media` image, the app, the
  annotator's Postgres and the model writer), every object named from the release; it reads the `sources` its values
  declare (`<release>-sources`, validated at render): a rask lakehouse by reference, any other source by URI or on a
  claim (at a `path` on it), so it runs with no rask at all. Development only, `demo` renders the claim and the Job
  (the media image, which carries the seeder) that fills it once for the demo source.
- `deploy/`: the development estate's overlay (`values-dev.yaml`), the clients it supplies to rask's dev Dex
  (`dex-client.yaml`), `fixtures/openfga`, the workbench's own development OpenFGA, and `fixtures/model-stand-in`, a stand-in model endpoint that speaks the model protocol and holds no model (the annotator's image run as `python -m annotator.model_endpoint.stand_in`; `make local` runs the same on port 8706). `docs/DEPLOY.md` says how a
  release is installed on the development cluster (`make deploy-dev`) and on a platform.
- `docs/model-protocol.md` and `docs/model-protocol-openapi.json`: the protocol the workbench calls a model endpoint by, published (`make model-protocol` writes the second from the stand-in, and a test holds them equal).
- `docs/rask/`: rask's published contract, vendored (`PROVENANCE.md` says from which sha and how to refresh it).
- `GLOSSARY.md` (the glossary), `docs/adr/` (the decisions), `docs/agents/` (how the workflow's skills use this
  repository) and `docs/planning/` (the inputs a planning session starts from): see § Agent skills.
- `scripts/`: dev and ops scripts.

Workspace membership is globbed: `[tool.uv.workspace] members = ["packages/*", "services/*"]` and
`frontend/package.json` `workspaces = ["app", "annotator", "packages/*"]`. A new Python member must ship a
`pyproject.toml` (uv refuses the workspace otherwise); a new JS package must ship a `package.json` (bun silently skips
it otherwise). Add a new Python import name to ruff's `known-first-party` in the same change.

## Git

- Commit messages are Conventional Commits (the commit-msg hook checks it) and carry no `Co-Authored-By: Claude` line
  (the hook refuses it).
- A commit names each row it carries with `Refs #n`, never `Closes`, `Fixes` or `Resolves`, which would close the
  issue on push.

## Agent skills

### Issue tracker

GitHub Issues on `AI-Riksarkivet/workbench`, one issue per row, titled `WB-1NN: …` (from `WB-101`; `WB-A` to `WB-E`
and `WB-1` to `WB-11` are the ids already taken), ordered by one pinned map issue (`wayfinder:map`). See
`docs/agents/issue-tracker.md`.

### Workflow (the mattpocock skills, plugin `mattpocock-skills`)

Work moves from idea to shipped along one path. Every step holds its output to the four facts in § Engineering
principles, GitOps first.

1. `/grill-with-docs` sharpens an idea by interview, and records terms in `GLOSSARY.md` and decisions as ADRs in
   `docs/adr/`. The input for the first session is `docs/planning/INPUT-2026-11-20.md`.
2. `/to-spec` writes the spec as an issue; `/to-tickets` splits it into rows with native blocking edges, each a
   sub-issue of the spec and placed in the map.
3. A row is built test-first (`/tdd` at the agreed seams), with the type checker and single test files as it goes and
   the full suite once, then reviewed by the Standards and the Spec reviewers in parallel (as `/code-review` does), every
   finding in the row's own code fixed, then committed, deployed and read back.
4. `/triage` admits a row nobody on the team wrote, including every defect a live walk finds; `/diagnosing-bugs` takes
   a hard bug to a red test first.

**Who works a row** (owner, 2026-11-20): the agent works rows without waiting for `/implement`, by the rules in
`docs/agents/issue-tracker.md`. `/grill-with-docs`, `/to-spec`, `/to-tickets` and `/triage` are started by the owner.

**Every row, whoever works it, follows the collaboration rules in `docs/agents/issue-tracker.md`:**

- **Claim first.** `gh issue edit <n> --add-assignee @me` is the first write, before a branch or a test.
- **No unnamed sub-steps inside an umbrella issue.** A piece of work is a row, or it waits for one. A spec's or a
  parent's comments record decisions and landed rows, never a backlog.
- **Coherent rows ship together, straight to `main`**, with no pull request: one review round, one commit naming each
  row with `Refs #n`, the gates once, one push, one deploy, a read-back proving each row's closes-when.
- **A row closes by hand** once its fix is deployed to the dev cluster and read back live, with a comment naming the
  workbench release's helm revision.

**Run the planning skills from this repository.** `/grill-with-docs`, `/to-spec`, `/to-tickets` and `/triage` write
`GLOSSARY.md`, ADRs and issues relative to the session's working directory: started in a session whose working directory
is rask's checkout, they write into rask. Start them only in a session opened in a workbench checkout.

### Triage labels

The five default roles (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`) and the
categories `bug` and `enhancement`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `GLOSSARY.md` plus `docs/adr/`. The installed skills call the glossary `GLOSSARY.md`; here it is
`GLOSSARY.md`, and no `GLOSSARY.md` is created. See `docs/agents/domain.md`.
