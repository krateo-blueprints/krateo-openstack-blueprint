{{/*
Horizon's MariaDB password (Krateo blueprint addition, issue #228, per-service MariaDB users). Django reads its
database settings from local_settings, whose DATABASES['default'] carried the horizon user's password as a literal:
OpenStack-Helm's published default.

With endpoints.oslo_db.auth.horizon.passwordFrom (store, secret, key: set by the umbrella), the password is read
through External Secrets Operator into the Secret horizon-db-password (one file, password), mounted read-only at
/etc/openstack-dashboard/krateo-db-password/ in every pod that reads local_settings, and local_settings' one
'PASSWORD' line becomes
  'PASSWORD': open('/etc/openstack-dashboard/krateo-db-password/password', encoding='utf-8').read().strip(),
the way SECRET_KEY is read (templates/_secret_key.tpl), so the published value renders nowhere. db-init reads the
same root through horizon-db-user (DB_CONNECTION), an ExternalSecret with the reference.

A local_settings template that does not hold exactly one 'PASSWORD': '<the values password>', line is refused with
the reference: a password written into the template itself would survive it.
*/}}

{{- define "horizon.db_password_from" -}}
{{- include "helm-toolkit.utils.db_from_root" (tuple . "oslo_db" "horizon") -}}
{{- end -}}

{{- define "horizon.db_password_dir" -}}/etc/openstack-dashboard/krateo-db-password{{- end -}}
{{- define "horizon.db_password_line" -}}'PASSWORD': open('{{ include "horizon.db_password_dir" . }}/password', encoding='utf-8').read().strip(),  # generated per install (#228){{- end -}}

{{/* local_settings with each generated credential read from its file: SECRET_KEY (secret_keyFrom) and the database
     password (the horizon MariaDB user's passwordFrom). */}}
{{- define "horizon.local_settings_from_roots" -}}
{{- $t := "" -}}
{{- if include "horizon.secret_key_from" . }}
{{- $t = include "horizon.local_settings_without_secret_key" . -}}
{{- else }}
{{- $t = include "horizon.local_settings_text" . -}}
{{- end }}
{{- if include "horizon.db_password_from" . }}
{{- $line := printf "'PASSWORD': '%s'," (.Values.endpoints.oslo_db.auth.horizon.password | toString) -}}
{{- if ne (len (splitList $line $t)) 2 }}
{{- fail (printf "endpoints.oslo_db.auth.horizon.passwordFrom is set, but local_settings does not hold exactly one %q line (DATABASES['default'], from endpoints.oslo_db.auth.horizon.password): a password written into conf.horizon.local_settings.template would survive the generated one (#228)" "'PASSWORD': '<password>',") }}
{{- end }}
{{- $t = replace $line (include "horizon.db_password_line" .) $t -}}
{{- end }}
{{- $t -}}
{{- end -}}

{{- define "horizon.db_password_externalsecret" -}}
{{- if include "horizon.db_password_from" . }}
{{- $from := .Values.endpoints.oslo_db.auth.horizon.passwordFrom -}}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index $from $field) }}
{{- fail (printf "endpoints.oslo_db.auth.horizon.passwordFrom.%s is required" $field) }}
{{- end }}
{{- end }}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: horizon-db-password
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
    name: horizon-db-password
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      data:
        password: {{ "{{ .password }}" | quote }}
  data:
    - secretKey: password
      remoteRef:
        key: {{ $from.secret | quote }}
        property: {{ $from.key | quote }}
{{- end }}
{{- end -}}
