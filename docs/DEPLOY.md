# Deploying the workbench

The workbench is one Helm chart, `chart/`: the app, the annotator, the viewer and search, the annotator's Postgres
(a CloudNativePG `Cluster`) and the job that writes the workbench's authorization model. Everything it relies on and does
not render is named by reference: the identity provider, an OpenFGA, the sources it reads, the Gateway, the secrets. Every object
is named from the release (`<release>-annotator`, `<release>-viewer`, `<release>-search`, `<release>-app`,
`<release>-annotator-state`).

Prerequisites on any cluster: the Gateway API with a Gateway the routes attach to, the CloudNativePG operator
(`postgresql.cnpg.io/v1`), and, for the development secrets only, the External Secrets Operator's `Password` generator
(`generators.external-secrets.io/v1alpha1`). Give the release a namespace of its own: the chart's NetworkPolicies are
the only thing keeping other pods away from its database where no other policy of the namespace admits them.

## The development cluster

The development estate is rask's k3s: rask in `default`, its Dex fixture (`dex`), Traefik's Gateway `traefik-gateway`
in `kube-system`, the registry the node pulls from as `localhost:5000`. The workbench goes beside it, in the namespace
`workbench`, at `http://workbench.localhost:8080` through the port-forward to the Gateway that also serves rask's console
at `http://localhost:8080`.

```bash
make deploy-dev          # every step below, in order; each is safe to run again
```

| Step | What it does on the cluster | Undo |
| --- | --- | --- |
| `make dev-images` | builds `media`, `annotator` and `frontend` through Dagger (the shared engine) and publishes them as `workbench-media`, `workbench-annotator`, `workbench-app` tagged with the commit (`git rev-parse --short=12 HEAD`) to `172.17.0.1:5000`; refuses a dirty tree | the tags stay in the registry until rask's `make registry-gc` |
| `make dev-openfga` | release `workbench-openfga` (`deploy/fixtures/openfga`) in `workbench`, creating the namespace: OpenFGA v1.18.3 on a Postgres `Cluster` of its own, its database password and preshared key generated in the cluster | `make dev-undeploy-openfga` |
| `make dev-model-stand-in` | release `workbench-model-stand-in` (`deploy/fixtures/model-stand-in`) in `workbench`: the annotator's image at the commit, run as the stand-in model endpoint (`python -m annotator.model_endpoint.stand_in`), which speaks the model protocol and holds no model; `deploy/values-dev.yaml` names it as `models.endpoint.url` | `make dev-undeploy-model-stand-in` |
| `make dev-dex-client` | rask's `scripts/fixtures.sh dex` with `deploy/dex-client.yaml`: rask's development Dex gains the public client `workbench-app` (PKCE, redirect `http://workbench.localhost:8080/auth/callback`), the confidential client `workbench-services`, and the CORS origin `http://workbench.localhost:8080`; Dex restarts once when its configuration changes | `make dev-undeploy-dex-client` |
| `make dev-release` | release `workbench` from `chart/` with `deploy/values-dev.yaml` and the commit's image tags (`--reset-values`, nothing read back from a release): the four workloads, the annotator's `Cluster`, the HTTPRoutes under `workbench.localhost`, the NetworkPolicies, the model writer job, the development Secrets, and the demo source (below) | `make dev-undeploy-release` (the demo claim stays, until the namespace goes) |
| `make dev-grants` | at rask's catalog doors, as a development administrator (`DEV_ADMIN`, its password from `DEV_ADMIN_PASSWORD_FILE` or asked): grants the workbench's own identity `dex-client~workbench-services` `reader` on the warehouse `acme`, for the cells a signed media link names | `make dev-undeploy-grants` |
| `make dev-readback` | waits for every Deployment and `Cluster` in `workbench` and asks the Gateway for the app's `/config.json` under `workbench.localhost` | none needed |

`make undeploy-dev` revokes the grants, uninstalls both releases, deletes the namespace (and with it both databases'
claims) and installs rask's Dex again without the workbench's clients. It leaves the images in the registry, and any
table a publish wrote in the lakehouse, which is its owner's to drop at the catalog's drop door.

Rask's own `make k3s-up` installs its Dex with rask's values alone, so it drops the workbench's clients: run
`make dev-dex-client` again after it.

The development Dex keeps its state in memory, so every restart signs with new keys and every token signed before is
unverifiable. Each service's background refresher reads the issuer's key set every 60 s (`KEY_SET_MAX_AGE_SECONDS`,
`workbench.governed.oidc`), so the viewer, search and the annotator all refuse such a token within about a minute of
the restart. Until a service has read the new set its reads still succeed. The first request naming the old key after
that read asks the issuer once more (the forced read below); a second read running without the key retires it there
(`RETIRE_AFTER_READS`: two reads, because replicas of one issuer can serve different sets for a while, and an issuer
that serves a set without a key it still signs with for two reads running has that key refused until a read holds it
again), so that request is refused 401 and every one after it at once, with no read and no wait (remembered 24 h, at
most 32 per issuer, `RETIRED_KID_SECONDS`, `RETIRED_KIDS_PER_ISSUER`); the next periodic read retires it as well. Where
another token spent the forced read's pace in the last 30 s, that first request is told to come back (503,
`Retry-After`, at most 30 s), which the app waits out. So a person whose page is open sees one prompt to sign in again
on the first read a service refuses, over the page as it was (what the page showed stays behind the prompt), and "Sign
in again" brings them back to the same path, query and hash (the canvas keeps its unsaved edits). A service pod that started after the restart never saw the old key, so it cannot know it was
withdrawn: the old token's first read there asks the issuer once (below) and is refused 401, or, with that read's pace
spent, is told to come back (503, `Retry-After`), which the app waits out at most 3 times and 30 s before the prompt.
Requests never read the issuer themselves: while it cannot be reached, a service goes on with
the last key set it read for up to 300 s from that read (`KEY_SET_STALE_LIMIT_SECONDS`) and then answers 503 at once;
the refresher tries again every 5 s (`FETCH_RETRY_SECONDS`), each attempt given 5 s in all (`FETCH_DEADLINE_SECONDS`;
a read that drips or does not end, in its TLS handshake, its headers or its body, is cut there by a watchdog that
shuts its connection down (the connect itself is bounded per address the issuer's name resolves to, at most what was
left of the deadline for each), and one past 1 MiB, `MAX_DOCUMENT_BYTES`, is cut at the cap; an
issuer that holds one connection open is read on a new one at the next attempt, with at most 2 abandoned reads,
`ABANDONED_READS`, running per issuer). A pod that starts while the issuer is down becomes ready (`/livez` and
`/readyz` read no token), answers 503 for tokens until the refresher's first good read, and recovers on its own. A
person who signs in again right after the restart holds tokens signed by the new key, which a service whose refresher
has not read it yet does not hold: the first such request asks the issuer once (at most once per 30 s,
`UNKNOWN_KID_REFRESH_INTERVAL_SECONDS`), and every such request arriving while that read is in flight, up to 8 per
service (`FORCED_WAITERS`), waits for it at most 5 s and is let in. If that read fails (the issuer still coming up),
the pace was spent by another token, or the read it joined began before the key was published, the request is answered
503 with `Retry-After`, never 401: the app sends the read again after it, and the refresher asks the issuer again after
5 s, so the page recovers without a second prompt. A token is refused 401 for its key only when a read of the issuer
that started after it asked found no such key.

A reload can sign the person in again from the issuer's own login session, with no form (`prompt=none`, ADR 0020,
`app.oidc.silentSignIn`), but only at an issuer that keeps one. The dev Dex, v2.46.1 as rask's fixture runs it, keeps
none: its sessions (with `prompt=none` answered from them, `id_token_hint`, and RP-initiated logout at an
`end_session_endpoint`) arrived in v2.46.0 behind the feature flag `DEX_SESSIONS_ENABLED`, off by default, and the
fixture sets none. Without it Dex does not act on `prompt=none` at login (v2.46.1 `server/authflow/login.go:102`): it
shows its form, then decides consent (`server/authflow/dispatch.go`). The fixture's `skipApprovalScreen: true` issues the
code then; with Dex's default (`false`) consent is never stored without sessions, so a request that carried
`prompt=none` is refused `interaction_required` after the form and the app asks again: two forms on every reload. So
`deploy/values-dev.yaml` turns silent sign-in **off**, and on the dev cluster today **every reload shows Dex's form
once**, and after it the person is back at the address they reloaded. Sign-out finds no end-session endpoint in its
discovery, so it sends `prompt=login`, which shows the form too. To keep the session across a reload there, rask's Dex
fixture (`deploy/fixtures/dex`, rask's repository) needs:

- the environment variable `DEX_SESSIONS_ENABLED=true` on the `dex` container;
- a `sessions:` block in its configuration (Dex's defaults are `cookieName: dex_session`, `absoluteLifetime: 24h`,
  `validIfNotUsedFor: 1h`), whose session cookie Dex sets `HttpOnly`, `SameSite=Lax` and on the issuer's path;
- its `clients` seam passing each client's `postLogoutRedirectURIs` through, and this repository's
  `deploy/dex-client.yaml` then naming `postLogoutRedirectURIs: [http://workbench.localhost:8080/]` for `workbench-app`:
  with sessions on, Dex advertises `end_session_endpoint`, the app's sign-out uses it, and Dex refuses a
  `post_logout_redirect_uri` its client did not register (an error page, and the session not ended);

and then `app.oidc.silentSignIn: true` in `deploy/values-dev.yaml`.

### The demo source

Beside rask's tables, the development release reads demo data of its own, so the cluster shows something whatever the
lakehouse holds: the corpus, recordings and films `make local` seeds (`demo~demo$corpus`, `demo~demo$recordings`,
`demo~demo$films`), as the source `demo` (`kind: namespace`, a directory catalog). `demo.enabled` renders a
PersistentVolumeClaim `workbench-demo` (k3s's default `local-path` class, 1Gi) and a Job `workbench-demo-seed-<digest>`
that runs `scripts/seed_demo_corpus.py --once` from the media image into it; the viewer, search and the annotator mount
the claim read-only, and only the Job mounts it writable. The Job is a plain object of the release, named by the digest
of its whole rendered pod template (which a Job cannot change in place), so an upgrade that changes nothing in it leaves
it as it is, and one that changes anything starts a new Job that writes only the demo datasets the volume lacks (each
written dataset has its marker under `.seeded.d/` on the volume) and leaves every other as it is. The claim carries
`helm.sh/resource-policy: keep`: an uninstall leaves the data, a reinstall from git adopts it, and only deleting the
claim (or `make undeploy-dev`, which deletes the namespace) seeds again. Its readers are four of the dev Dex's demo
people (alice, bob, carol and dave, as `dex~<user id>`), granted `reader` on `source:demo` by the model writer; erin is
left out on purpose, so a signed-in person who is not a reader is refused the demo's list entry, its rows, its signed
links and its search, and that refusal can be read back on the cluster.

The demo source is read-only, as every source but a rask lakehouse is, and marked `annotatable: true`, so its items are
sent to a project, labelled and reviewed: each records the table it was read from and the version the sender saw, and
the work people do on it stays in the annotator's Postgres, as all work in progress does, until the project is
published.

### Whose grants are whose

The workbench's services read datasets as the person: under Dex, the person's own token is passed on (it names the
lakehouse's audience), so what a person may read is their own grants, made by their project's administrator at rask's
doors. `make dev-grants` grants people nothing. It grants the workbench's own identity only what it uses as itself:
reading the one cell a signed media link names. It writes nothing in the lakehouse while people label; a publish writes
the project's table in its target namespace, which the project's administrator grants it (`writer` there) at rask's
doors.

## On a platform

A platform runs its own identity provider, OpenFGA and secret store, and the chart names them:

- **Sign-in:** `auth.oidc.issuer`, `idpId` (the lakehouse's idp id for the same issuer, so a grant in either names the
  same person), `audience`, `publicOrigin`, and `dpopRequired` (on where the provider binds tokens). Register a public
  client for the app (`app.oidc.clientId`, PKCE, redirect `<publicOrigin>/auth/callback`) and, where the services
  write to or read the lakehouse as themselves, a confidential client (`auth.oidc.client.id`) whose secret is a Secret
  the platform provides (`auth.oidc.client.secret.name`).
- **Staying signed in across a reload:** `app.oidc.silentSignIn` (on by default) makes the app's first sign-in in a
  tab ask with `prompt=none`, so an issuer holding the person's login session signs them in again with no form. The
  provider must keep a login session of its own and answer `prompt=none` from it (OIDC Core § 3.1.2.1), returning
  `login_required` or `interaction_required` to the callback when it cannot; for sign-out to end that session, it must
  advertise an `end_session_endpoint` (OpenID Connect RP-Initiated Logout 1.0) and accept the app's root,
  `<publicOrigin>/`, as a registered post-logout redirect (Keycloak: the client's "Valid post logout redirect URIs").
  **Turn it off for an issuer that keeps no session of its own** (Dex without `DEX_SESSIONS_ENABLED`): off, no sign-in
  sends `prompt=none`, and every reload shows the issuer's form once. Off is otherwise the same as on: sign-out is
  remembered in the browser and sends `prompt=login` from then on, and a page restored from the back/forward cache
  while its sign-in was in flight loads again and starts that sign-in over. What a person sees on a reload:

  | Issuer | `silentSignIn: true` | `silentSignIn: false` |
  | --- | --- | --- |
  | Keycloak (a session, `prompt=none` honoured) | no form while its session lasts; its form once after | the same: it answers an ordinary request from its session too, but may show a page of its own (consent, an account chooser) where `prompt=none` guarantees none |
  | Dex with `DEX_SESSIONS_ENABLED` | no form while its session lasts; its form once after | the same, as for Keycloak |
  | Dex without it, `skipApprovalScreen: true` | its form once | its form once |
  | Dex without it, `skipApprovalScreen: false` (Dex's default) | its form, then its form again and its approval screen | its form once, then its approval screen |

  In every case the person comes back to the address they reloaded. A browser that withholds the issuer's cookie is an
  issuer with no session. An issuer that answers `prompt=none` with an error page of its own is left by Back, and the
  app, reloaded or restored from the back/forward cache, then asks the person, once. Sign-out records in this browser the id
  of the last sign-out (never removed), and a completed sign-in the id it answered; until they match no sign-in is
  silent and each sends `prompt=login`, another tab still signed in drops its tokens and shows the one prompt to sign in
  again over the page as it was (nothing leaves until the person chooses, and the canvas keeps its unsaved edits), and a
  sign-in sent before the last sign-out is discarded at its callback. Neither value is a credential.
- **The sources:** `sources` lists what the release reads, rendered into the file every service reads
  (`<release>-sources`) and checked at render: a DNS-label `name` each, at most one `kind: rask`, and no property that
  names a credential. A lakehouse is `kind: rask` with its catalog's `uri`; `auth.lakehouse.audience` names its audience
  and `auth.lakehouse.delegation` how a person's read reaches it (`exchange` at a provider that exchanges its own tokens,
  `forward` where it does not), and further issuers each name their own (`auth.oidc.providers[].lakehouse`). Only the
  lakehouse is sent a token and published to.
- **Authorization:** `auth.fga.apiUrl`, `auth.fga.store`, and the Secret holding the preshared key
  (`auth.fga.credential`).
- **Models:** the workbench deploys none (ADR 0023). `models.endpoint.url` names the model endpoint assist asks: a Ray Serve deployment that speaks the workbench's model protocol, at its route prefix behind Serve's HTTP proxy (`http://<rayservice>-serve-svc:8000/<route prefix>`; ADR 0032, `docs/model-protocol.md` § A Ray Serve deployment); `models.endpoint.audience` is the audience of the ServiceAccount token the annotator presents to it (projected, an hour, read on every call), which the deployment verifies itself against the cluster issuer, with its subject the annotator's ServiceAccount (`system:serviceaccount:<namespace>:<release>-annotator`), since Serve verifies no token; `models.endpoint.timeoutSeconds` is how long an ask may take. With no `url` the release has no assist and the canvas says so. Each model's weights carry their own licence, checked before use.
- **Secrets:** `media.viewer.mediaUrlKey` names the viewer's signing key; `dev.secrets.enabled` stays off, so git holds
  no secret's value.
- **Routes:** `route.parentRefs` and `route.hostnames`; `networkPolicy.enabled` with `apiServerEgress` for the API
  server as a policy sees it.

## Without rask, or beside it

The workbench reads any Lance data a release declares, read-only, beside a lakehouse or with none:

- `kind: namespace`: a lance-namespace catalog by `impl` (`dir` or `rest`) and its `properties`; with `claim`, a
  PersistentVolumeClaim mounted read-only as a `dir` catalog's `root` (the directory `path` names on it, when it names
  one). A `rest` catalog names the store prefixes its
  tables may lie under (`locations`): a table it places anywhere else is refused, and its tables are read on the
  `storage` options the release declares, never on what the catalog answers.
- `kind: tables`: a bare Lance table (a URI ending `.lance`) or a directory of them, by `uri`, or by `path` on a
  `claim`.

```yaml
sources:
  - { name: archive, title: Field archive, kind: namespace, impl: dir, claim: archive-catalog, readers: { orgs: [acme] } }
  - name: scans
    kind: tables
    uri: s3://scans/2026.lance
    storage:
      region: eu-north-1
      aws_role_arn: arn:aws:iam::123456789012:role/workbench-scans
      aws_web_identity_token_file: /var/run/secrets/workbench/store/token
    readers: { people: [corp~ada] }
storeIdentity: { audience: sts.amazonaws.com }
```

**Who decides a read.** A lakehouse read as the person (`identity: person`) decides every read itself, on the person's
token. Every other source (a `namespace` or `tables` source, and a lakehouse read as the workbench, `identity: service`)
knows nobody, so the workbench's own OpenFGA decides: the model writer grants `reader` on `source:<name>` to exactly the
organisations and people its `readers` names, and removes what git no longer declares. A source with no `readers` is
read by nobody. Every read of such a source is checked as it resolves the source it reads, the default dataset included,
whatever else the request names (the dataset list omits it; a descriptor, a page of rows, a row, a search, a page, a
signed link's mint and its serve refuse it, 403), and a signed link stops serving once the person it was signed for is
no longer a reader. What such a read answers is cached by the reader's own browser alone (`Cache-Control: private`). The viewer and search ask the same store as the annotator: beside such a
source the chart gives each of them `auth.fga` (the store's settings and its preshared key, mounted), and a service that
signs people in refuses to start beside such a source without the store, rather than let every check allow, and refuses a
`rask` directory stand-in (`identity: none`) outright. A store or model not there by the boot deadline is looked for again
while the service runs, its doors answering 503 until it is found. Only with sign-in off (the local stack, where
everyone is `anon`) does every check allow.

**No credential in git.** A source's properties and storage options are held to an allow-list
(`chart/files/source-keys.json`, the same list the services enforce, `workbench.source_keys`): no key, token, signature,
header or proxy, no location with a user, password or query, and a credential file only under `/var/run/secrets/`. A
store is reached on the workload's own identity: with `storeIdentity.audience` named, the viewer and search mount their
ServiceAccount's token for that audience at `/var/run/secrets/workbench/store/token`, which the source's `storage`
names (S3 web identity, or Azure's `azure_federated_token_file`, `lance_docs/lance/guide/object_store.md`); the store
trusts the cluster's issuer for that role. With none, the store is reached only as the platform otherwise grants the pod.

**What is labelled.** A lakehouse's datasets always are. Any other source's are only when git marks it
`annotatable: true` (owner, 2026-11-17: "Only if marked"); an unmarked one's datasets say "source '<name>' is not marked
annotatable" on every send control, and the annotator refuses to send from it. A marked source's items record their
table and version at send, as a lakehouse's do; a publish names only the lakehouse's tables as its lineage inputs (a
commit names only its own lakehouse's tables), so under the run rule a project holding such items is refused with that
reason, and without it the publish lands naming no input from that source. Every published row names the table and
version its item was read from (`item_table`, `item_table_version`), whatever the source. Labeling needs no lakehouse:
the work people do is kept in the annotator's own Postgres until a publish, so a release with no `kind: rask` source
labels every source marked annotatable, and its publish is refused with "this deployment reads no rask lakehouse, so
there is nowhere to publish to". A dataset with no workbench descriptor is shown as a plain table; one whose writer stamped a
`lance_media.descriptor` keeps every view it declares.

`make chart-check` renders the chart in each of these shapes: the dev overlay (its demo source among them), a platform's,
a release with no lakehouse (a directory catalog on a claim, marked annotatable, and a bare table on a store), and one with sign-in off
reading one table on a claim.
