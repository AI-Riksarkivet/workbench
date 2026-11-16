#!/usr/bin/env bash
# The chart's refusals, rendered: each misconfiguration a template refuses (its `fail`, or a `required` value left out) is
# rendered over the development overlay, and the render must fail with the template's own message. A refusal that stops
# firing, or fires with another message, fails `make chart-check`, so a misconfiguration never reaches a cluster as a
# release that renders.
#
#   bash scripts/chart-refusals.sh        # from the repository root; needs helm
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

APIS=(--api-versions postgresql.cnpg.io/v1/Cluster --api-versions generators.external-secrets.io/v1alpha1/Password)
BASE=(-f deploy/values-dev.yaml)
failed=0
checked=0

# refuses <the message's text> <helm args beyond the base...>: the render fails, and says so in the template's words.
refuses() {
  local message="$1"
  shift
  local out
  checked=$((checked + 1))
  if out="$(helm template workbench chart -n workbench "$@" 2>&1)"; then
    echo "NOT REFUSED: $message" >&2
    echo "  rendered with: $*" >&2
    failed=$((failed + 1))
  elif ! grep -qF -- "$message" <<<"$out"; then
    echo "REFUSED WITH ANOTHER MESSAGE: expected \"$message\"" >&2
    echo "  rendered with: $*" >&2
    echo "  said: $(grep -m1 -i 'error' <<<"$out")" >&2
    failed=$((failed + 1))
  fi
}

# The overlay itself renders: every refusal below is the one change named.
helm template workbench chart -n workbench "${APIS[@]}" "${BASE[@]}" >/dev/null

# The platform's prerequisites, named when a cluster does not serve them.
refuses "CloudNativePG is a prerequisite" --api-versions generators.external-secrets.io/v1alpha1/Password "${BASE[@]}"
refuses "dev.secrets.enabled generates keys with the External Secrets Operator's Password generator" \
  --api-versions postgresql.cnpg.io/v1/Cluster "${BASE[@]}"
refuses "media.viewer.mediaUrlKey.name: the Secret holding the viewer's media-URL signing key" \
  "${APIS[@]}" "${BASE[@]}" --set media.viewer.mediaUrlKey.name=
refuses "dev.secrets.oidcClientSecret: the development identity provider's secret" \
  "${APIS[@]}" "${BASE[@]}" --set dev.secrets.oidcClientSecret=

# The routes and the network.
refuses "route.parentRefs must name the Gateway" "${APIS[@]}" "${BASE[@]}" --set route.parentRefs=null
refuses "route.doors: /api/../x is not a path" "${APIS[@]}" "${BASE[@]}" --set 'route.doors[0]=/api/../x'
refuses "networkPolicy.apiServerEgress must name the Kubernetes API server's endpoints" \
  "${APIS[@]}" "${BASE[@]}" --set networkPolicy.apiServerEgress=null
refuses "forwardedAllowIps must not be '*'" "${APIS[@]}" "${BASE[@]}" --set 'forwardedAllowIps=*'
refuses "lifecycle: need teardownSeconds < terminationGracePeriodSeconds - preStopSeconds" \
  "${APIS[@]}" "${BASE[@]}" --set lifecycle.teardownSeconds=60

# The images.
refuses "image.tag must be set" "${APIS[@]}" "${BASE[@]}" --set image.tag=
refuses "app.image.tag must be set" "${APIS[@]}" "${BASE[@]}" --set app.image.tag=

# Sign-in.
refuses "auth.oidc.issuer: the provider's issuer" "${APIS[@]}" "${BASE[@]}" --set auth.oidc.issuer=
refuses "auth.oidc.audience: the workbench's own audience" "${APIS[@]}" "${BASE[@]}" --set auth.oidc.audience=
refuses "auth.oidc.publicOrigin: the origin the browser addresses the workbench by" \
  "${APIS[@]}" "${BASE[@]}" --set auth.oidc.publicOrigin=
refuses "auth.oidc.idpId=kubernetes is not a person's idp id" "${APIS[@]}" "${BASE[@]}" --set auth.oidc.idpId=kubernetes
refuses "auth.oidc.connectors.local=svc-client is not a person's idp id" \
  "${APIS[@]}" "${BASE[@]}" --set auth.oidc.connectors.local=svc-client
refuses "auth.oidc.subjectClaims are never read beside auth.oidc.connectors" \
  "${APIS[@]}" "${BASE[@]}" --set 'auth.oidc.subjectClaims[0]=sub'
refuses "auth.oidc.providers: http://other.example is not https and is not declared allow_insecure" \
  "${APIS[@]}" "${BASE[@]}" --set 'auth.oidc.providers[0].issuer=http://other.example'
refuses "auth.oidc.providers: https://other.example passes its people's tokens on (forward)" \
  "${APIS[@]}" "${BASE[@]}" --set auth.oidc.dpopRequired=true \
  --set 'auth.oidc.providers[0].issuer=https://other.example' --set 'auth.oidc.providers[0].lakehouse.delegation=forward'
refuses "auth.oidc.connectors names people by Dex's federated_claims, which a token carries only when app.oidc.scopes asks for federated:id" \
  "${APIS[@]}" "${BASE[@]}" --set 'app.oidc.scopes={openid}'
refuses "auth.oidc.client.secret.name: the Secret holding the workbench client's secret" \
  "${APIS[@]}" "${BASE[@]}" --set auth.oidc.client.secret.name=
refuses "auth.fga.apiUrl: the platform's OpenFGA" "${APIS[@]}" "${BASE[@]}" --set auth.fga.apiUrl=
refuses "otel.endpoint: the OTLP/HTTP endpoint telemetry is sent to" "${APIS[@]}" "${BASE[@]}" --set otel.endpoint=

# The sources: each a name and a kind, no credential, at most one lakehouse.
refuses "sources: declare at least one source the workbench reads" "${APIS[@]}" "${BASE[@]}" --set sources=null
refuses 'sources[0].name "Lake" is not a DNS label' "${APIS[@]}" "${BASE[@]}" --set 'sources[0].name=Lake'
refuses 'sources: the name "lake" is declared twice' "${APIS[@]}" "${BASE[@]}" \
  --set 'sources[1].name=lake' --set 'sources[1].kind=tables' --set 'sources[1].uri=s3://b/t.lance'
refuses 'sources[0].kind "parquet" is rask, namespace or tables' "${APIS[@]}" "${BASE[@]}" --set 'sources[0].kind=parquet'
refuses "sources: at most one source is a rask lakehouse" "${APIS[@]}" "${BASE[@]}" \
  --set 'sources[1].name=other' --set 'sources[1].kind=rask' --set 'sources[1].uri=http://other.example'
refuses "sources[0].uri: the lakehouse catalog's REST door" "${APIS[@]}" "${BASE[@]}" --set 'sources[0].uri=null'
refuses "sources[0].identity is person or service" "${APIS[@]}" "${BASE[@]}" --set 'sources[0].identity=none'
refuses "sources[0].readers: a lakehouse read as the person decides every read itself" \
  "${APIS[@]}" "${BASE[@]}" --set 'sources[0].readers.orgs[0]=acme'
refuses "sources[0]: a rask lakehouse is reached at its catalog's \`uri\`, never on a claim" \
  "${APIS[@]}" "${BASE[@]}" --set 'sources[0].claim=lake'
refuses "sources[1].impl: the lance-namespace implementation" "${APIS[@]}" "${BASE[@]}" \
  --set 'sources[1].name=ns' --set 'sources[1].kind=namespace'
refuses 'sources[1].impl "nope" takes no declared property a source may name' "${APIS[@]}" "${BASE[@]}" \
  --set 'sources[1].name=ns' --set 'sources[1].kind=namespace' --set 'sources[1].impl=nope'
refuses "sources[1].properties.password is not one a dir source may declare" "${APIS[@]}" "${BASE[@]}" \
  --set 'sources[1].name=ns' --set 'sources[1].kind=namespace' --set 'sources[1].impl=dir' \
  --set 'sources[1].properties.root=/data' --set 'sources[1].properties.password=x'
refuses "sources[1]: a dir source names its \`root\`, or a \`claim\`" "${APIS[@]}" "${BASE[@]}" \
  --set 'sources[1].name=ns' --set 'sources[1].kind=namespace' --set 'sources[1].impl=dir'
refuses "sources[1].locations: the store prefixes its tables may lie under" "${APIS[@]}" "${BASE[@]}" \
  --set 'sources[1].name=ns' --set 'sources[1].kind=namespace' --set 'sources[1].impl=rest' \
  --set 'sources[1].properties.uri=https://ns.example'
refuses "sources[1]: a tables source names its \`uri\`, or a \`path\` on its \`claim\`" "${APIS[@]}" "${BASE[@]}" \
  --set 'sources[1].name=t' --set 'sources[1].kind=tables'
refuses "carries a user or password: a source declares no credential" "${APIS[@]}" "${BASE[@]}" \
  --set 'sources[1].name=t' --set 'sources[1].kind=tables' --set 'sources[1].uri=s3://me:pw@bucket/t.lance'
refuses "carries a query or fragment, as a pre-signed URL does" "${APIS[@]}" "${BASE[@]}" \
  --set 'sources[1].name=t' --set 'sources[1].kind=tables' --set 'sources[1].uri=s3://bucket/t.lance?X-Amz-Signature=1'
refuses "names a credential file outside" "${APIS[@]}" "${BASE[@]}" \
  --set 'sources[1].name=t' --set 'sources[1].kind=tables' --set 'sources[1].uri=s3://bucket/t.lance' \
  --set 'sources[1].storage.aws_web_identity_token_file=/etc/token'
refuses "is not a storage option a source may declare" "${APIS[@]}" "${BASE[@]}" \
  --set 'sources[1].name=t' --set 'sources[1].kind=tables' --set 'sources[1].uri=s3://bucket/t.lance' \
  --set 'sources[1].storage.aws_secret_access_key=x'

if [ "$failed" -gt 0 ]; then
  echo "$failed of $checked refusals did not fire as declared" >&2
  exit 1
fi
echo "the chart refuses all $checked misconfigurations, each with its own message"
