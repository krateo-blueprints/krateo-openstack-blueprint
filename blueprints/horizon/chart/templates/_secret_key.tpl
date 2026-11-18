{{/*
Horizon's SECRET_KEY (Krateo blueprint addition, issue #228). Django signs session cookies and CSRF
tokens with it; OpenStack-Helm's published value lets anyone forge a session for any user.

With endpoints.dashboard.auth.secret_keyFrom (store, secret, key: set by the umbrella), the key is read
through External Secrets Operator into the Secret horizon-secret-key (one file, secret_key), mounted
read-only at /etc/openstack-dashboard/krateo-secret-key/ in every pod that reads local_settings, and
local_settings' one SECRET_KEY line becomes
  SECRET_KEY = open('/etc/openstack-dashboard/krateo-secret-key/secret_key', encoding='utf-8').read().strip()
so the published value renders nowhere.

0.3.61 delivered the key as a local_settings.d snippet instead. Horizon executes only the snippets its start
script links into openstack_dashboard/local/local_settings.d, and that one was not linked: with no SECRET_KEY
in local_settings, settings.py generated a random key per pod in /tmp (measured: each pod a different key, not
the root). Setting it in local_settings itself never reaches that fallback.
Rotating it signs everyone out once: sessions signed with the old key are rejected.
*/}}

{{- define "horizon.secret_key_from" -}}
{{- with ((.Values.endpoints.dashboard | default dict).auth | default dict).secret_keyFrom }}{{ if .secret }}true{{ end }}{{ end -}}
{{- end -}}

{{- define "horizon.secret_key_dir" -}}/etc/openstack-dashboard/krateo-secret-key{{- end -}}
{{- define "horizon.secret_key_line" -}}SECRET_KEY = open('{{ include "horizon.secret_key_dir" . }}/secret_key', encoding='utf-8').read().strip()  # generated per install (#228){{- end -}}

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
{{- replace $line (printf "%s\n" (include "horizon.secret_key_line" .)) $t -}}
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
  # OnChange (#309 C): refreshed only when this ExternalSecret itself changes, never on a timer, so a
  # root edited by hand (which ESO regenerates) reaches no live credential until a rotation.
  refreshPolicy: OnChange
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
        secret_key: {{ "{{ .password }}" | quote }}
  data:
    - secretKey: password
      remoteRef:
        key: {{ $from.secret | quote }}
        property: {{ $from.key | quote }}
{{- end }}
{{- end -}}
