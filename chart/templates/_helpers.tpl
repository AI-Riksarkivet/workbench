{{- /* Every object is named from the release (`<release>-<component>`) and labelled with the workbench's name, the
release and its component, so two releases in one namespace never share a name or a selector. */ -}}
{{- define "annotator.name" -}}{{ printf "%s-annotator" .Release.Name }}{{- end -}}

{{- define "annotator.selector" -}}
app.kubernetes.io/name: workbench
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: annotator
{{- end -}}

{{- /* The demo volume's claim (`demo`), which the demo source names as its `claim`. */ -}}
{{- define "workbench.demoClaim" -}}{{ printf "%s-demo" .Release.Name }}{{- end -}}

{{- define "annotator.stateCluster" -}}{{ printf "%s-state" (include "annotator.name" .) }}{{- end -}}

{{- define "annotator.otlpEndpoint" -}}{{ required "otel.endpoint: the OTLP/HTTP endpoint telemetry is sent to" .Values.otel.endpoint }}{{- end -}}

{{- /* The sources the services read (`client_kit.sources`), validated here so a release that could not be read as
written fails at render: each a DNS-label `name`, one of three kinds, at most one `rask` lakehouse (by its catalog's REST
`uri`, read as the person or as the workbench, never as nobody), a `namespace` by its lance-namespace `impl`, a `tables`
source by its `uri` or by a `path` on its `claim`. A declaration carries no credential: every property and storage
option must be on the allow-list `files/source-keys.json` holds (the services enforce the same list,
`client_kit.source_keys`, and `make chart-check` fails when the two differ), a credential file must lie under its mount
root, and no location carries a user, a password, a query or a fragment. A source the workbench decides for (any but a
lakehouse read as the person) names its `readers`, organisations and principal keys the model writer grants `reader` on
`source:<name>`; with none, nobody reads it. A source with a `claim` reads a PersistentVolumeClaim mounted read-only at
`/var/lib/workbench/sources/<name>`: a `namespace` source's `dir` root, or a `tables` source's directory, at the
`path` it names on the claim (never outside it). A lakehouse's datasets are always annotated; any other source's only
when it is marked `annotatable: true` (a boolean, `client_kit.sources.annotatable`), with or without a lakehouse
beside it: work in progress is the annotator's own, and only a publish needs a lakehouse. The declaration
rendered is the file every service reads (`WORKBENCH_SOURCES_FILE`). */ -}}
{{- define "workbench.sources" -}}
{{- $keys := .Files.Get "files/source-keys.json" | fromJson -}}
{{- $declared := list -}}
{{- $names := dict -}}
{{- $lakehouses := 0 -}}
{{- range $i, $s := .Values.sources -}}
{{- if not (regexMatch "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$" (toString $s.name)) }}{{ fail (printf "sources[%d].name %q is not a DNS label" $i (toString $s.name)) }}{{ end -}}
{{- if hasKey $names $s.name }}{{ fail (printf "sources: the name %q is declared twice" $s.name) }}{{ end -}}
{{- $_ := set $names $s.name true -}}
{{- $out := dict "name" $s.name "kind" $s.kind "title" ($s.title | default "") -}}
{{- $mount := printf "/var/lib/workbench/sources/%s" $s.name -}}
{{- if and $s.path (not $s.claim) }}{{ fail (printf "sources[%d].path is a directory on the source's `claim`, and it names none" $i) }}{{ end -}}
{{- if regexMatch "(^|/)\\.\\.(/|$)" (toString ($s.path | default "")) }}{{ fail (printf "sources[%d].path %q leaves its claim" $i (toString $s.path)) }}{{ end -}}
{{- $decides := true -}}
{{- if eq $s.kind "rask" -}}
{{- $lakehouses = add1 $lakehouses -}}
{{- $_ := set $out "uri" (required (printf "sources[%d].uri: the lakehouse catalog's REST door" $i) $s.uri) -}}
{{- include "workbench.checkLocation" (list (printf "sources[%d].uri" $i) $s.uri) -}}
{{- $_ := set $out "identity" ($s.identity | default "person") -}}
{{- if not (has $out.identity (list "person" "service")) }}{{ fail (printf "sources[%d].identity is person or service: a lakehouse's REST door is never read as nobody" $i) }}{{ end -}}
{{- $decides = eq $out.identity "service" -}}
{{- if and $s.readers (not $decides) }}{{ fail (printf "sources[%d].readers: a lakehouse read as the person decides every read itself" $i) }}{{ end -}}
{{- if $s.delimiter }}{{ $_ := set $out "delimiter" $s.delimiter }}{{ end -}}
{{- else if eq $s.kind "namespace" -}}
{{- $_ := set $out "impl" (required (printf "sources[%d].impl: the lance-namespace implementation" $i) $s.impl) -}}
{{- $allowed := index $keys.namespaceProperties $s.impl | default list -}}
{{- if not (hasKey $keys.namespaceProperties $s.impl) }}{{ fail (printf "sources[%d].impl %q takes no declared property a source may name: one of %s" $i $s.impl (keys $keys.namespaceProperties | sortAlpha | join ", ")) }}{{ end -}}
{{- $properties := deepCopy ($s.properties | default dict) -}}
{{- if $s.claim }}{{ $_ := set $properties "root" (printf "%s/%s" $mount ($s.path | default "" | trimPrefix "/") | trimSuffix "/") }}{{ end -}}
{{- range $k, $v := $properties }}
{{- if not (has $k $allowed) }}{{ fail (printf "sources[%d].properties.%s is not one a %s source may declare (allowed: %s)" $i $k $s.impl (join ", " $allowed)) }}{{ end -}}
{{- if has $k (list "uri" "root") }}{{ include "workbench.checkLocation" (list (printf "sources[%d].properties.%s" $i $k) $v) }}{{ end -}}
{{- end -}}
{{- if and (eq $s.impl "dir") (not (hasKey $properties "root")) }}{{ fail (printf "sources[%d]: a dir source names its `root`, or a `claim`" $i) }}{{ end -}}
{{- $locations := $s.locations | default list -}}
{{- if and (ne $s.impl "dir") (empty $locations) }}{{ fail (printf "sources[%d].locations: the store prefixes its tables may lie under" $i) }}{{ end -}}
{{- range $locations }}{{ include "workbench.checkLocation" (list (printf "sources[%d].locations" $i) .) }}{{ end -}}
{{- include "workbench.checkStorage" (list $keys (printf "sources[%d].storage" $i) ($s.storage | default dict)) -}}
{{- $_ := set $out "properties" $properties -}}
{{- $_ := set $out "storage" ($s.storage | default dict) -}}
{{- if $locations }}{{ $_ := set $out "locations" $locations }}{{ end -}}
{{- else if eq $s.kind "tables" -}}
{{- $uri := ternary (printf "%s/%s" $mount ($s.path | default "" | trimPrefix "/")) ($s.uri | default "") (not (empty $s.claim)) | trimSuffix "/" -}}
{{- if empty $uri }}{{ fail (printf "sources[%d]: a tables source names its `uri`, or a `path` on its `claim`" $i) }}{{ end -}}
{{- include "workbench.checkLocation" (list (printf "sources[%d].uri" $i) $uri) -}}
{{- include "workbench.checkStorage" (list $keys (printf "sources[%d].storage" $i) ($s.storage | default dict)) -}}
{{- $_ := set $out "uri" $uri -}}
{{- $_ := set $out "storage" ($s.storage | default dict) -}}
{{- else -}}
{{- fail (printf "sources[%d].kind %q is rask, namespace or tables" $i (toString $s.kind)) -}}
{{- end -}}
{{- if and $s.claim (eq $s.kind "rask") }}{{ fail (printf "sources[%d]: a rask lakehouse is reached at its catalog's `uri`, never on a claim" $i) }}{{ end -}}
{{- if hasKey $s "annotatable" -}}
{{- if eq $s.kind "rask" }}{{ fail (printf "sources[%d].annotatable: a rask lakehouse's datasets are always annotated" $i) }}{{ end -}}
{{- if not (kindIs "bool" $s.annotatable) }}{{ fail (printf "sources[%d].annotatable is true or false, not %v" $i $s.annotatable) }}{{ end -}}
{{- $_ := set $out "annotatable" $s.annotatable -}}
{{- end -}}
{{- if and $decides $s.readers }}{{ $_ := set $out "readers" (dict "orgs" ($s.readers.orgs | default list) "people" ($s.readers.people | default list)) }}{{ end -}}
{{- $declared = append $declared $out -}}
{{- end -}}
{{- if empty $declared }}{{ fail "sources: declare at least one source the workbench reads" }}{{ end -}}
{{- if gt $lakehouses 1 }}{{ fail "sources: at most one source is a rask lakehouse" }}{{ end -}}
{{- toJson (dict "sources" $declared) -}}
{{- end -}}

{{- /* Fails the render when a location carries a user, a password, a query or a fragment: a credential travels in each. */ -}}
{{- define "workbench.checkLocation" -}}
{{- $where := index . 0 -}}{{- $location := toString (index . 1) -}}
{{- if regexMatch "^[A-Za-z][A-Za-z0-9+.-]*://[^/]*@" $location }}{{ fail (printf "%s carries a user or password: a source declares no credential" $where) }}{{ end -}}
{{- if regexMatch "[?#]" $location }}{{ fail (printf "%s carries a query or fragment, as a pre-signed URL does: a source declares no credential" $where) }}{{ end -}}
{{- end -}}

{{- /* Fails the render on a storage option not on the allow-list, or a credential file outside its mount root; a per-base
option (`base_<id>.<key>`) is held to its key's rule. */ -}}
{{- define "workbench.checkStorage" -}}
{{- $keys := index . 0 -}}{{- $where := index . 1 -}}
{{- range $k, $v := index . 2 -}}
{{- $name := regexReplaceAll "^base_[0-9]+\\." $k "" -}}
{{- if has $name $keys.mountedFiles -}}
{{- if or (not (hasPrefix $keys.mountRoot (toString $v))) (contains "/../" (printf "%s/" $v)) }}{{ fail (printf "%s.%s names a credential file outside %s: mount it there" $where $k $keys.mountRoot) }}{{ end -}}
{{- else if not (has $name $keys.storage) -}}
{{- fail (printf "%s.%s is not a storage option a source may declare (no credential is declared; see client_kit.source_keys)" $where $k) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- /* The rask lakehouse source, as JSON (`{}` when the release reads none). */ -}}
{{- define "workbench.lakehouse" -}}
{{- $found := dict -}}
{{- range .Values.sources }}{{ if eq .kind "rask" }}{{ $found = . }}{{ end }}{{ end -}}
{{- toJson $found -}}
{{- end -}}

{{- define "workbench.sourcesEnv" -}}
- { name: WORKBENCH_SOURCES_FILE, value: /etc/workbench/sources/sources.json }
{{- end -}}

{{- define "workbench.sourcesMount" -}}
- { name: sources, mountPath: /etc/workbench/sources, readOnly: true }
{{- range .Values.sources }}
{{- if .claim }}
- { name: {{ printf "source-%s" .name }}, mountPath: {{ printf "/var/lib/workbench/sources/%s" .name }}, readOnly: true }
{{- end }}
{{- end }}
{{- end -}}

{{- define "workbench.sourcesVolume" -}}
- name: sources
  configMap: { name: {{ printf "%s-sources" .Release.Name }} }
{{- range .Values.sources }}
{{- if .claim }}
- name: {{ printf "source-%s" .name }}
  persistentVolumeClaim: { claimName: {{ .claim | quote }}, readOnly: true }
{{- end }}
{{- end }}
{{- end -}}

{{- /* Whether the lakehouse authenticates the services' reads: a rask source, with `auth.lakehouse.audience` named. */ -}}
{{- define "workbench.lakehouseAuth" -}}{{ $lake := include "workbench.lakehouse" . | fromJson }}{{ if and .Values.auth.enabled $lake.uri .Values.auth.lakehouse.audience }}true{{ end }}{{- end -}}

{{- /* Whether a declared source is the workbench's own to authorize: any but a lakehouse read as the person, read only
by its declared `readers` (`client_kit.sources.workbench_decides`). The viewer and search then need the workbench's
store as the annotator does, and with sign-in on a service declaring one refuses to start without it. */ -}}
{{- define "workbench.decidesASource" -}}
{{- range (include "workbench.sources" . | fromJson).sources }}{{ if or (ne .kind "rask") (eq .identity "service") }}true{{ end }}{{ end -}}
{{- end -}}

{{- /* The workbench's own OpenFGA store, as the annotator, its model writer, and the viewer and search beside a source the
workbench authorizes (`workbench.decidesASource`) reach it. */ -}}
{{- define "workbench.fgaEnv" -}}
- { name: WORKBENCH_FGA_ENABLED, value: "true" }
- { name: WORKBENCH_FGA_API_URL, value: {{ required "auth.fga.apiUrl: the platform's OpenFGA" .Values.auth.fga.apiUrl | quote }} }
- { name: WORKBENCH_FGA_STORE, value: {{ .Values.auth.fga.store | quote }} }
{{- if .Values.auth.fga.credential.name }}
- { name: WORKBENCH_FGA_CREDENTIAL_FILE, value: /var/run/secrets/workbench/fga/preshared-key }
{{- end }}
{{- end -}}

{{- /* The URL the browser addresses a service by, which each DPoP proof's `htu` names: the public origin and the
service's route prefix. */ -}}
{{- define "workbench.publicUrl" -}}
{{- $root := index . 0 -}}{{- $prefix := index . 1 -}}
{{- printf "%s%s" (required "auth.oidc.publicOrigin: the origin the browser addresses the workbench by" $root.Values.auth.oidc.publicOrigin | trimSuffix "/") $prefix -}}
{{- end -}}

{{- define "workbench.idpId" -}}
{{- $setting := index . 0 -}}{{- $idp := index . 1 -}}
{{- if or (not (kindIs "string" $idp)) (eq $idp "kubernetes") (hasSuffix "-client" $idp) (not (regexMatch "^[a-z0-9-]+$" $idp)) }}{{ fail (printf "%s=%v is not a person's idp id: lower-case letters, digits and '-', never kubernetes and never ending in -client, which name machines" $setting $idp) }}{{ end -}}
{{- end -}}

{{- /* Sign-in, as every service verifies it: the provider, how its people are named (as the lakehouse names them,
rask's ADR 0208), and whether a token must come with a DPoP proof. `list root publicUrl`. */ -}}
{{- define "workbench.oidcEnv" -}}
{{- $root := index . 0 -}}{{- $publicUrl := index . 1 -}}
{{- $o := $root.Values.auth.oidc -}}
{{- $issuer := required "auth.oidc.issuer: the provider's issuer, which a token's iss must equal" $o.issuer -}}
{{- $idp := required "auth.oidc.idpId: the idp id the provider's people are principals of" $o.idpId -}}
{{- include "workbench.idpId" (list "auth.oidc.idpId" $idp) -}}
{{- range $connector, $mapped := $o.connectors }}{{ include "workbench.idpId" (list (printf "auth.oidc.connectors.%s" $connector) $mapped) }}{{ end -}}
{{- if and $o.connectors $o.subjectClaims }}{{ fail "auth.oidc.subjectClaims are never read beside auth.oidc.connectors, which name people by Dex's federated_claims" }}{{ end -}}
{{- range $o.providers }}
{{- if and (not (hasPrefix "https://" .issuer)) (not .allow_insecure) }}{{ fail (printf "auth.oidc.providers: %s is not https and is not declared allow_insecure (a development provider only)" .issuer) }}{{ end -}}
{{- if and .lakehouse (eq (.lakehouse.delegation | default "") "forward") $o.dpopRequired }}{{ fail (printf "auth.oidc.providers: %s passes its people's tokens on (forward), which a DPoP-bound token never is: set auth.oidc.dpopRequired=false or exchange" .issuer) }}{{ end -}}
{{- end -}}
- { name: WORKBENCH_AUDIT_ENABLED, value: {{ $root.Values.auth.audit | quote }} }
- { name: WORKBENCH_OIDC_ENABLED, value: "true" }
- { name: WORKBENCH_OIDC_ISSUER, value: {{ $issuer | quote }} }
{{- with $o.discoveryUrl }}
- { name: WORKBENCH_OIDC_DISCOVERY_URL, value: {{ . | quote }} }
{{- end }}
- { name: WORKBENCH_OIDC_AUDIENCE, value: {{ required "auth.oidc.audience: the workbench's own audience" $o.audience | quote }} }
- { name: WORKBENCH_OIDC_IDP_ID, value: {{ $idp | quote }} }
{{- with $o.subjectClaims }}
- { name: WORKBENCH_OIDC_SUBJECT_CLAIMS, value: {{ toJson . | quote }} }
{{- end }}
{{- with $o.connectors }}
- { name: WORKBENCH_OIDC_CONNECTORS, value: {{ toJson . | quote }} }
{{- end }}
{{- with $o.clientMarker }}
- { name: WORKBENCH_OIDC_CLIENT_MARKER, value: {{ toJson . | quote }} }
{{- end }}
{{- with $o.providers }}
- { name: WORKBENCH_OIDC_PROVIDERS, value: {{ toJson . | quote }} }
{{- end }}
- { name: WORKBENCH_OIDC_ALLOW_INSECURE, value: {{ ternary "false" "true" (hasPrefix "https://" $issuer) | quote }} }
- { name: WORKBENCH_DPOP_REQUIRED, value: {{ $o.dpopRequired | quote }} }
- { name: WORKBENCH_PUBLIC_URL, value: {{ $publicUrl | quote }} }
- { name: WORKBENCH_DPOP_WINDOW_SECONDS, value: {{ $o.dpopWindowSeconds | quote }} }
{{- end -}}

{{- /* The app's DPoP mode: the services' own where it is not named. A deployment whose services require a proof makes
the app refuse a provider that offers no DPoP, and never lets it sign in without binding. */ -}}
{{- define "workbench.appDpop" -}}
{{- $mode := .Values.app.oidc.dpop | default (ternary "required" "offered" .Values.auth.oidc.dpopRequired) -}}
{{- if not (has $mode (list "required" "offered" "off")) }}{{ fail (printf "app.oidc.dpop=%v: required, offered or off" $mode) }}{{ end -}}
{{- if and .Values.auth.oidc.dpopRequired (ne $mode "required") }}{{ fail (printf "app.oidc.dpop=%s beside auth.oidc.dpopRequired: the services refuse every token without its proof, so the app must require DPoP" $mode) }}{{ end -}}
{{- $mode -}}
{{- end -}}

{{- /* How a person's read reaches the lakehouse, and the workbench's confidential client for its own token. */ -}}
{{- define "workbench.lakehouseEnv" -}}
{{- $l := .Values.auth.lakehouse -}}
{{- if not (has $l.delegation (list "exchange" "forward")) }}{{ fail (printf "auth.lakehouse.delegation=%v: exchange (the provider exchanges its own tokens, RFC 8693) or forward (the person's token already names the lakehouse's audience)" $l.delegation) }}{{ end -}}
{{- if and (eq $l.delegation "forward") .Values.auth.oidc.dpopRequired }}{{ fail "auth.lakehouse.delegation=forward passes the person's token on, which a DPoP-bound token never is: set auth.oidc.dpopRequired=false for a provider that binds no token, or exchange" }}{{ end -}}
- { name: WORKBENCH_OIDC_CLIENT_ID, value: {{ required "auth.oidc.client.id: the workbench's confidential client" .Values.auth.oidc.client.id | quote }} }
- { name: WORKBENCH_OIDC_CLIENT_SECRET_FILE, value: /var/run/secrets/workbench/oidc-client/client-secret }
- { name: WORKBENCH_LAKEHOUSE_AUDIENCE, value: {{ required "auth.lakehouse.audience: the lakehouse's audience" $l.audience | quote }} }
- { name: WORKBENCH_LAKEHOUSE_DELEGATION, value: {{ $l.delegation | quote }} }
{{- with $l.audienceScope }}
- { name: WORKBENCH_LAKEHOUSE_AUDIENCE_SCOPE, value: {{ . | quote }} }
{{- end }}
{{- end -}}

{{- define "annotator.authEnv" -}}
{{ include "workbench.oidcEnv" (list . (include "workbench.publicUrl" (list . .Values.route.prefix))) }}
{{- if include "workbench.lakehouseAuth" . }}
{{ include "workbench.lakehouseEnv" . }}
{{- end }}
{{ include "workbench.fgaEnv" . }}
{{- end -}}
