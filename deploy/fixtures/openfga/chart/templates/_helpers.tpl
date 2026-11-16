{{- define "openfga.labels" -}}
app.kubernetes.io/name: openfga
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: workbench
{{- end -}}

{{- define "openfga.database" -}}{{ printf "%s-db" .Release.Name }}{{- end -}}
