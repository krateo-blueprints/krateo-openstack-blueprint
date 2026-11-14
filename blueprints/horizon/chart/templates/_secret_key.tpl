{{/*
Horizon's SECRET_KEY (Krateo blueprint addition, issue #228). Django signs session cookies and CSRF
tokens with it; OpenStack-Helm's published value lets anyone forge a session for any user.

With endpoints.dashboard.auth.secret_keyFrom (store, secret, key: set by the umbrella), the key is read
through External Secrets Operator into the snippet local_settings.d/_90_krateo_secret_key.py (Horizon
executes local_settings.d after local_settings), and local_settings is rendered exactly as upstream
except that its one SECRET_KEY line is replaced by a comment, so the published value renders nowhere.
Rotating it signs everyone out once: sessions signed with the old key are rejected.
*/}}

{{- define "horizon.secret_key_from" -}}
{{- with ((.Values.endpoints.dashboard | default dict).auth | default dict).secret_keyFrom }}{{ if .secret }}true{{ end }}{{ end -}}
{{- end -}}

{{- define "horizon.secret_key_snippet_name" -}}_90_krateo_secret_key.py{{- end -}}

{{/* local_settings as helm-toolkit.snippets.values_template_renderer renders it for a Secret. */}}
{{- define "horizon.local_settings_text" -}}
{{- $t := tpl (.Values.conf.horizon.local_settings.template | toYaml) . -}}
{{- if hasPrefix "|\n" $t }}{{- $t = regexReplaceAllLiteral "\n  " ($t | trimPrefix "|\n" | trimPrefix "  ") "\n" -}}{{- end -}}
{{- $t -}}
{{- end -}}

{{/* local_settings without its SECRET_KEY line (with the reference). */}}
{{- define "horizon.local_settings_without_secret_key" -}}
{{- $t := include "horizon.local_settings_text" . -}}
{{- $line := printf "SECRET_KEY='%s'\n" (.Values.conf.horizon.local_settings.config.horizon_secret_key | toString) -}}
{{- if ne (len (splitList $line $t)) 2 }}
{{- fail (printf "cannot remove SECRET_KEY from local_settings: expected exactly one %q line" "SECRET_KEY='<value>'") }}
{{- end }}
{{- replace $line (printf "# SECRET_KEY is set by local_settings.d/%s (generated per install, #228)\n" (include "horizon.secret_key_snippet_name" .)) $t -}}
{{- end -}}

{{- define "horizon.secret_key_externalsecret" -}}
{{- if include "horizon.secret_key_from" . }}
{{- $from := .Values.endpoints.dashboard.auth.secret_keyFrom -}}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index $from $field) }}
{{- fail (printf "endpoints.dashboard.auth.secret_keyFrom.%s is required" $field) }}
{{- end }}
{{- end }}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: horizon-secret-key
  annotations:
    # Deleted by helm WITH the release, after the pre-delete hooks (#254).
    krateo.io/teardown: with-release
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: SecretStore
    name: {{ $from.store | quote }}
  target:
    name: horizon-secret-key
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      data:
        {{ include "horizon.secret_key_snippet_name" . }}: {{ "SECRET_KEY = '{{ .password }}'\n" | quote }}
  data:
    - secretKey: password
      remoteRef:
        key: {{ $from.secret | quote }}
        property: {{ $from.key | quote }}
{{- end }}
{{- end -}}
