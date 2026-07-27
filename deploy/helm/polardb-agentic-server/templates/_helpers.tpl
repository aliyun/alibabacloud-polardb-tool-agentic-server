{{- define "polardb-agentic-server.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "polardb-agentic-server.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "polardb-agentic-server.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "polardb-agentic-server.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
app.kubernetes.io/name: {{ include "polardb-agentic-server.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "polardb-agentic-server.selectorLabels" -}}
app.kubernetes.io/name: {{ include "polardb-agentic-server.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "polardb-agentic-server.image" -}}
{{- if .Values.image.digest -}}
{{ printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else -}}
{{ printf "%s:%s" .Values.image.repository .Values.image.tag }}
{{- end -}}
{{- end }}

{{- define "polardb-agentic-server.secretName" -}}
{{- required "existingSecret must name a Secret containing PAS_DATABASE_URL and PAS_ENCRYPTION_KEY" .Values.existingSecret -}}
{{- end }}

{{- define "polardb-agentic-server.securityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop:
    - ALL
{{- end }}

{{- define "polardb-agentic-server.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
- name: log
  mountPath: /app/log
- name: runtime
  mountPath: /var/run/pas
{{- end }}

{{- define "polardb-agentic-server.volumes" -}}
- name: tmp
  emptyDir: {}
- name: log
  emptyDir: {}
- name: runtime
  emptyDir: {}
{{- end }}
