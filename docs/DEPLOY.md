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
| `make dev-dex-client` | rask's `scripts/fixtures.sh dex` with `deploy/dex-client.yaml`: rask's development Dex gains the public client `workbench-app` (PKCE, redirect `http://workbench.localhost:8080/auth/callback`), the confidential client `workbench-services`, and the CORS origin `http://workbench.localhost:8080`; Dex restarts once when its configuration changes | `make dev-undeploy-dex-client` |
| `make dev-release` | release `workbench` from `chart/` with `deploy/values-dev.yaml` and the commit's image tags (`--reset-values`, nothing read back from a release): the four workloads, the annotator's `Cluster`, the HTTPRoutes under `workbench.localhost`, the NetworkPolicies, the model writer job, the development Secrets, and the demo source (below) | `make dev-undeploy-release` (the demo claim stays, until the namespace goes) |
| `make dev-grants` | at rask's catalog doors, as a development administrator (`DEV_ADMIN`, its password from `DEV_ADMIN_PASSWORD_FILE` or asked): grants the workbench's own identity `dex-client~workbench-services` `reader` on the warehouse `acme`, for the cells a signed media link names | `make dev-undeploy-grants` |
| `make dev-readback` | waits for every Deployment and `Cluster` in `workbench` and asks the Gateway for the app's `/config.json` under `workbench.localhost` | none needed |

`make undeploy-dev` revokes the grants, uninstalls both releases, deletes the namespace (and with it both databases'
claims) and installs rask's Dex again without the workbench's clients. It leaves the images in the registry, and any
table a publish wrote in the lakehouse, which is its owner's to drop at the catalog's drop door.

Rask's own `make k3s-up` installs its Dex with rask's values alone, so it drops the workbench's clients: run
`make dev-dex-client` again after it.

### The demo source

Beside rask's tables, the development release reads demo data of its own, so the cluster shows something whatever the
lakehouse holds: the corpus, recordings and films `make local` seeds (`demo~demo$corpus`, `demo~demo$recordings`,
`demo~demo$films`), as the source `demo` (`kind: namespace`, a directory catalog). `demo.enabled` renders a
PersistentVolumeClaim `workbench-demo` (k3s's default `local-path` class, 1Gi) and a Job `workbench-demo-seed-<digest>`
that runs `scripts/seed_demo_corpus.py --once` from the media image into it; the viewer, search and the annotator mount
the claim read-only, and only the Job mounts it writable. The Job is a plain object of the release, named by the digest
of its whole rendered pod template (which a Job cannot change in place), so an upgrade that changes nothing in it leaves
it as it is, and one that changes anything starts a new Job that finds the completed seed (`.seeded` on the volume) and
exits 0 without writing. The claim carries
`helm.sh/resource-policy: keep`: an uninstall leaves the data, a reinstall from git adopts it, and only deleting the
claim (or `make undeploy-dev`, which deletes the namespace) seeds again. Its readers are the dev Dex's demo people
(alice, bob, carol, dave and erin, as `dex~<user id>`), granted `reader` on `source:demo` by the model writer.

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
- **The sources:** `sources` lists what the release reads, rendered into the file every service reads
  (`<release>-sources`) and checked at render: a DNS-label `name` each, at most one `kind: rask`, and no property that
  names a credential. A lakehouse is `kind: rask` with its catalog's `uri`; `auth.lakehouse.audience` names its audience
  and `auth.lakehouse.delegation` how a person's read reaches it (`exchange` at a provider that exchanges its own tokens,
  `forward` where it does not), and further issuers each name their own (`auth.oidc.providers[].lakehouse`). Only the
  lakehouse is sent a token and published to.
- **Authorization:** `auth.fga.apiUrl`, `auth.fga.store`, and the Secret holding the preshared key
  (`auth.fga.credential`).
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
read by nobody. Every door that touches such a source asks first (the dataset list omits it; a descriptor, a page of rows,
a row, a search, a page, a signed link's mint and its serve refuse it, 403), and a signed link stops serving once the
person it was signed for is no longer a reader. With sign-in off (the local stack) every check allows.

**No credential in git.** A source's properties and storage options are held to an allow-list
(`chart/files/source-keys.json`, the same list the services enforce, `client_kit.source_keys`): no key, token, signature,
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
