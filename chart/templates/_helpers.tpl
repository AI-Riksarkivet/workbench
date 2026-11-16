{{- /* Every object is named from the release (`<release>-<component>`) and labelled with the workbench's name, the
release and its component, so two releases in one namespace never share a name or a selector. */ -}}
{{- define "annotator.name" -}}{{ printf "%s-annotator" .Release.Name }}{{- end -}}

{{- define "annotator.selector" -}}
app.kubernetes.io/name: workbench
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: annotator
{{- end -}}

{{- define "annotator.stateCluster" -}}{{ printf "%s-state" (include "annotator.name" .) }}{{- end -}}

{{- define "annotator.otlpEndpoint" -}}{{ required "otel.endpoint: the OTLP/HTTP endpoint telemetry is sent to" .Values.otel.endpoint }}{{- end -}}

{{- /* The catalog every service reads datasets through: a lance-namespace REST catalog (`catalog.uri`, a rask lakehouse's
or any other), or a directory catalog on a claim (`catalog.dir.claim`), mounted where `MEDIA_CATALOG_DIR` names. */ -}}
{{- define "workbench.catalogEnv" -}}
{{- if eq (empty .Values.catalog.uri) (empty .Values.catalog.dir.claim) }}{{ fail "name exactly one catalog: catalog.uri (a lance-namespace REST catalog) or catalog.dir.claim (a directory catalog on a PersistentVolumeClaim)" }}{{ end -}}
{{- if .Values.catalog.uri }}
- { name: MEDIA_CATALOG_URI, value: {{ .Values.catalog.uri | quote }} }
{{- else }}
- { name: MEDIA_CATALOG_DIR, value: /var/lib/workbench/catalog }
{{- end }}
{{- end -}}

{{- define "workbench.catalogMount" -}}
{{- if .Values.catalog.dir.claim }}
- { name: catalog, mountPath: /var/lib/workbench/catalog }
{{- end }}
{{- end -}}

{{- define "workbench.catalogVolume" -}}
{{- if .Values.catalog.dir.claim }}
- name: catalog
  persistentVolumeClaim: { claimName: {{ .Values.catalog.dir.claim | quote }} }
{{- end }}
{{- end -}}

{{- /* Whether the lakehouse authenticates the services' reads: a REST catalog with `auth.lakehouse.audience` named. */ -}}
{{- define "workbench.lakehouseAuth" -}}{{ if and .Values.auth.enabled .Values.catalog.uri .Values.auth.lakehouse.audience }}true{{ end }}{{- end -}}

{{- /* The workbench's sign-in and store settings, shared by the annotator and its model writer. */ -}}
{{- define "annotator.fgaEnv" -}}
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
- { name: ANNOTATOR_ANNOTATIONS_NAMESPACE, value: {{ required "auth.annotationsNamespace: the workbench's own annotations namespace" .Values.auth.annotationsNamespace | quote }} }
{{ include "annotator.fgaEnv" . }}
{{- end -}}
