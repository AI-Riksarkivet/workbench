{{- define "annotator.name" -}}{{ printf "%s-annotator" .Values.rask.release }}{{- end -}}

{{- define "annotator.selector" -}}
app.kubernetes.io/name: rask
app.kubernetes.io/instance: {{ .Values.rask.release }}
app.kubernetes.io/component: annotator
{{- end -}}

{{- define "annotator.stateCluster" -}}{{ printf "%s-state" (include "annotator.name" .) }}{{- end -}}

{{- define "annotator.secretStore" -}}{{ printf "%s-kubernetes" (include "annotator.name" .) }}{{- end -}}

{{- define "annotator.otlpEndpoint" -}}{{ .Values.otel.endpoint | default (printf "http://%s-otel-collector:4318" .Values.rask.release) }}{{- end -}}

{{- define "annotator.daprSpec" -}}
features:
  - name: HotReload
    enabled: false
{{- if .Values.otel.enabled }}
{{- $endpoint := include "annotator.otlpEndpoint" . }}
tracing:
  samplingRate: {{ .Values.otel.samplingRate | quote }}
  otel:
    endpointAddress: {{ regexReplaceAll "^https?://" $endpoint "" | quote }}
    isSecure: {{ hasPrefix "https://" $endpoint }}
    protocol: http
    timeout: "30s"
{{- end }}
metrics:
  enabled: true
  recordErrorCodes: true
  http:
    increasedCardinality: false
    pathMatching:
      - "/v1.0/actors/{actorType}/{actorId}/method/{method}"
logging:
  apiLogging:
    enabled: true
    obfuscateURLs: true
    omitHealthChecks: true
secrets:
  scopes:
    - storeName: {{ include "annotator.secretStore" . }}
      defaultAccess: deny
{{- end -}}

{{- define "annotator.daprConfigName" -}}
{{- printf "%s-%s" (include "annotator.name" .) (include "annotator.daprSpec" . | sha256sum | trunc 10) -}}
{{- end -}}


{{- /* The workbench's sign-in and store settings, shared by the annotator and its model writer. */ -}}
{{- define "annotator.fgaEnv" -}}
- { name: WORKBENCH_FGA_ENABLED, value: "true" }
- { name: WORKBENCH_FGA_API_URL, value: {{ required "auth.fga.apiUrl: the platform's OpenFGA" .Values.auth.fga.apiUrl | quote }} }
- { name: WORKBENCH_FGA_STORE, value: {{ .Values.auth.fga.store | quote }} }
{{- if .Values.auth.fga.credential.name }}
- { name: WORKBENCH_FGA_CREDENTIAL_FILE, value: /var/run/secrets/workbench/fga/preshared-key }
{{- end }}
{{- end -}}

{{- define "annotator.authEnv" -}}
{{- $o := .Values.auth.oidc -}}
{{- $issuer := required "auth.oidc.issuer: the provider's issuer, which a token's iss must equal" $o.issuer -}}
{{- $idp := required "auth.oidc.idpId: the idp id the provider's subjects are principals of" $o.idpId -}}
{{- if or (eq $idp "kubernetes") (hasSuffix "-client" $idp) (not (regexMatch "^[a-z0-9-]+$" $idp)) }}{{ fail (printf "auth.oidc.idpId=%s is not a person's idp id: lower-case letters, digits and '-', never kubernetes and never ending in -client, which name machines" $idp) }}{{ end -}}
- { name: WORKBENCH_AUDIT_ENABLED, value: {{ .Values.auth.audit | quote }} }
- { name: WORKBENCH_OIDC_ENABLED, value: "true" }
- { name: WORKBENCH_OIDC_ISSUER, value: {{ $issuer | quote }} }
{{- with $o.discoveryUrl }}
- { name: WORKBENCH_OIDC_DISCOVERY_URL, value: {{ . | quote }} }
{{- end }}
- { name: WORKBENCH_OIDC_AUDIENCE, value: {{ required "auth.oidc.audience: the workbench's own audience" $o.audience | quote }} }
- { name: WORKBENCH_OIDC_IDP_ID, value: {{ $idp | quote }} }
- { name: WORKBENCH_OIDC_ALLOW_INSECURE, value: {{ ternary "false" "true" (hasPrefix "https://" $issuer) | quote }} }
- { name: WORKBENCH_PUBLIC_URL, value: {{ required "auth.oidc.publicUrl: the URL the browser addresses this service by" $o.publicUrl | quote }} }
- { name: WORKBENCH_DPOP_WINDOW_SECONDS, value: {{ $o.dpopWindowSeconds | quote }} }
- { name: WORKBENCH_OIDC_CLIENT_ID, value: {{ required "auth.oidc.client.id: the workbench's confidential client" $o.client.id | quote }} }
- { name: WORKBENCH_OIDC_CLIENT_SECRET_FILE, value: /var/run/secrets/workbench/oidc-client/client-secret }
- { name: WORKBENCH_LAKEHOUSE_AUDIENCE, value: {{ required "auth.lakehouseAudience: the lakehouse's audience" .Values.auth.lakehouseAudience | quote }} }
- { name: ANNOTATOR_ANNOTATIONS_NAMESPACE, value: {{ required "auth.annotationsNamespace: the workbench's own annotations namespace" .Values.auth.annotationsNamespace | quote }} }
{{ include "annotator.fgaEnv" . }}
{{- end -}}
