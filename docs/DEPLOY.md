# Deploying the workbench

The workbench is one Helm chart, `chart/`: the app, the annotator, the viewer and search, the annotator's Postgres
(a CloudNativePG `Cluster`) and the job that writes the workbench's authorization model. Everything it relies on and does
not render is named by reference: the identity provider, an OpenFGA, a catalog, the Gateway, the secrets. Every object
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
| `make dev-release` | release `workbench` from `chart/` with `deploy/values-dev.yaml` and the commit's image tags (`--reset-values`, nothing read back from a release): the four workloads, the annotator's `Cluster`, the HTTPRoutes under `workbench.localhost`, the NetworkPolicies, the model writer job, and the development Secrets | `make dev-undeploy-release` |
| `make dev-grants` | at rask's catalog doors, as a development administrator (`DEV_ADMIN`, its password from `DEV_ADMIN_PASSWORD_FILE` or asked): creates the annotations namespace `acme$workbench_annotations` if missing, grants the workbench's own identity `dex-client~workbench-services` `writer` there and `reader` on the warehouse `acme` | `make dev-undeploy-grants` (the namespace and its tables stay) |
| `make dev-readback` | waits for every Deployment and `Cluster` in `workbench` and asks the Gateway for the app's `/config.json` under `workbench.localhost` | none needed |

`make undeploy-dev` revokes the grants, uninstalls both releases, deletes the namespace (and with it both databases'
claims) and installs rask's Dex again without the workbench's clients. It leaves the images in the registry and the
annotation tables in the lakehouse.

Rask's own `make k3s-up` installs its Dex with rask's values alone, so it drops the workbench's clients: run
`make dev-dex-client` again after it.

### Whose grants are whose

The workbench's services read datasets as the person: under Dex, the person's own token is passed on (it names the
lakehouse's audience), so what a person may read is their own grants, made by their project's administrator at rask's
doors. `make dev-grants` grants people nothing. It grants the workbench's own identity only what it uses as itself:
writing its annotation tables, and reading the one cell a signed media link names.

## On a platform

A platform runs its own identity provider, OpenFGA and secret store, and the chart names them:

- **Sign-in:** `auth.oidc.issuer`, `idpId` (the lakehouse's idp id for the same issuer, so a grant in either names the
  same person), `audience`, `publicOrigin`, and `dpopRequired` (on where the provider binds tokens). Register a public
  client for the app (`app.oidc.clientId`, PKCE, redirect `<publicOrigin>/auth/callback`) and, where the services
  write to or read the lakehouse as themselves, a confidential client (`auth.oidc.client.id`) whose secret is a Secret
  the platform provides (`auth.oidc.client.secret.name`).
- **The lakehouse:** `catalog.uri` names its catalog; `auth.lakehouse.audience` its audience and
  `auth.lakehouse.delegation` how a person's read reaches it (`exchange` at a provider that exchanges its own tokens,
  `forward` where it does not). Further issuers each name their own (`auth.oidc.providers[].lakehouse`).
- **Authorization:** `auth.fga.apiUrl`, `auth.fga.store`, and the Secret holding the preshared key
  (`auth.fga.credential`).
- **Secrets:** `media.viewer.mediaUrlKey` names the viewer's signing key; `dev.secrets.enabled` stays off, so git holds
  no secret's value.
- **Routes:** `route.parentRefs` and `route.hostnames`; `networkPolicy.enabled` with `apiServerEgress` for the API
  server as a policy sees it.

## Without rask

The workbench needs no lakehouse: `catalog.dir.claim` names a PersistentVolumeClaim holding a lance-namespace
directory catalog, mounted where every service reads it, and the release names no `catalog.uri` and no
`auth.lakehouse.*`. Sign-in and authorization are the workbench's own as ever; the services read the catalog with no
token, and the annotator writes its annotation tables into the same directory. A ReadWriteOnce claim keeps every service
on one node; ReadWriteMany spreads them.

```bash
helm upgrade --install workbench chart --namespace workbench --create-namespace \
  --set catalog.dir.claim=workbench-catalog --set auth.oidc.issuer=https://id.example ...
```

`make chart-check` renders the chart in each of these shapes.
