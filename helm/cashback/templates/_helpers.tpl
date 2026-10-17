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
     2. {image.registry}/{image.repository}/<component>
     The app registry is deliberately not global.imageRegistry: Bitnami
     subcharts apply that to their own images too (and it wins over their
     image.registry). global.imageRegistry, when set, overrides every
     registry (ours included) — the usual Bitnami meaning (a mirror).
     Tag defaults to .Chart.AppVersion. */}}
{{- define "cashback.image" -}}
{{- $registry := default .root.Values.image.registry .root.Values.global.imageRegistry -}}
{{- $defaultRepo := printf "%s/%s/%s" $registry .root.Values.image.repository .name -}}
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

{{/* Auth keys live in their own Secret (B19): the shared one above is
     mounted whole (envFrom) into every workload, this one only key by key
     (secretKeyRef) into the Deployments that need each key. */}}
{{- define "cashback.authSecretName" -}}
{{- printf "%s-auth" (include "cashback.fullname" .) -}}
{{- end -}}

{{/* A secret value from values.yaml: required, at least .min bytes, not a
     public placeholder (the services refuse those at startup anyway; this
     fails `helm install` before anything is rolled out).
     Usage: include "cashback.secretValue" (dict "key" "secrets.jwtSecret"
            "value" .Values.secrets.jwtSecret "min" 32) */}}
{{- define "cashback.secretValue" -}}
{{- $hint := printf "%s is required unless externalSecrets.enabled=true (e.g. openssl rand -hex 32)" .key -}}
{{- $value := required $hint .value | toString -}}
{{- if lt (len (trim $value)) (int .min) -}}
{{- fail (printf "%s must be at least %d bytes" .key (int .min)) -}}
{{- end -}}
{{- $placeholders := list "admin" "dev-secret-change-me" "please-change-me" "change-me" "changeme" "secret" "test-secret" -}}
{{- if has (lower (trim $value)) $placeholders -}}
{{- fail (printf "%s is a known default value" .key) -}}
{{- end -}}
{{- $value -}}
{{- end -}}

{{/* env entry reading one key of the auth Secret. */}}
{{- define "cashback.authSecretEnv" -}}
- name: {{ .key }}
  valueFrom:
    secretKeyRef:
      name: {{ include "cashback.authSecretName" .root }}
      key: {{ .key }}
{{- end -}}
