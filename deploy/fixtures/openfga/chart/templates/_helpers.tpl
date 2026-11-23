{{- define "openfga.labels" -}}
app.kubernetes.io/name: openfga
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: workbench
{{- end -}}
