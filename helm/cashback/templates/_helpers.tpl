{{/* =====================================================================
     Common helpers — names, labels, image refs.
     ===================================================================== */}}

{{/* Chart-derived release prefix. */}}
{{- define "cashback.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "cashback.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "cashback.serviceAccount" -}}
{{- default (include "cashback.fullname" .) .Values.global.serviceAccount.name -}}
{{- end -}}

{{/* Component-specific helpers. Pass the component values via $.Values.<name>. */}}
{{- define "cashback.componentName" -}}
{{- printf "%s-%s" (include "cashback.fullname" .root) .name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "cashback.commonLabels" -}}
app.kubernetes.io/name: {{ include "cashback.fullname" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ include "cashback.chart" .root }}
app.kubernetes.io/part-of: cashback
{{- if .component }}
app.kubernetes.io/component: {{ .component }}
{{- end }}
{{- end -}}

{{- define "cashback.componentSelector" -}}
app.kubernetes.io/name: {{ include "cashback.fullname" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* Resolve the image reference for a component. Order:
     1. <component>.image.repository if set (full path expected)
     2. {global.imageRegistry}/{global.imageRepository}/<component>
     Tag defaults to .Chart.AppVersion. */}}
{{- define "cashback.image" -}}
{{- $defaultRepo := printf "%s/%s/%s" .root.Values.global.imageRegistry .root.Values.global.imageRepository .name -}}
{{- $repo := default $defaultRepo .image.repository -}}
{{- $tag := default .root.Chart.AppVersion .image.tag -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}

{{/* ConfigMap reference name (one shared CM for env wiring). */}}
{{- define "cashback.configMapName" -}}
{{- printf "%s-config" (include "cashback.fullname" .) -}}
{{- end -}}

{{- define "cashback.secretName" -}}
{{- printf "%s-secrets" (include "cashback.fullname" .) -}}
{{- end -}}
