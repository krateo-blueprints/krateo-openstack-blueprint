{{/*
The MariaDB root password's Secrets (Krateo blueprint addition, issue #228).

Without endpoints.oslo_db.auth.admin.passwordFrom: OpenStack-Helm's Secrets, from values, under their
usual names. With it: ExternalSecrets projecting the root ESO generated for the install, under NEW
names. The new names are the rotation mechanism: start.py re-creates the root user from
MYSQL_DBADMIN_PASSWORD on every start of a server with an existing data directory
(CREATE OR REPLACE USER 'root'@'%' through mysqld --init-file, no old password needed), so rotating
means the server restarting with the new value. Pointing the StatefulSet's secretKeyRef at a new
name changes its pod spec -- it rolls exactly at the switch -- and the pod can only ever read the
generated value: it waits for ESO to write it instead of racing helm's deletion of the old Secret.
*/}}

{{- define "mariadb.root_from" -}}
{{- with .Values.endpoints.oslo_db.auth.admin.passwordFrom }}{{ if .secret }}true{{ end }}{{ end -}}
{{- end -}}

{{- define "mariadb.dbadmin_secret_name" -}}
{{- if include "mariadb.root_from" . }}mariadb-dbadmin-generated{{ else }}mariadb-dbadmin-password{{ end -}}
{{- end -}}

{{- define "mariadb.secrets_etc_name" -}}
{{- if include "mariadb.root_from" . }}mariadb-secrets-generated{{ else }}mariadb-secrets{{ end -}}
{{- end -}}

{{/* A .cnf rendered from values, with its `password = <value>` line turned into an ESO template. */}}
{{- define "mariadb.cnf_templated" -}}
{{- $file := index . 0 -}}{{- $envAll := index . 1 -}}
{{- $rendered := tuple $file $envAll | include "helm-toolkit.utils.template" -}}
{{- $line := printf "password = %s\n" ($envAll.Values.endpoints.oslo_db.auth.admin.password | toString) -}}
{{- if ne (len (splitList $line $rendered)) 2 }}
{{- fail (printf "cannot place the root password in %s: expected exactly one 'password = <value>' line" $file) }}
{{- end }}
{{- replace $line "password = {{ .password }}\n" $rendered -}}
{{- end -}}

{{- define "mariadb.root_externalsecret" -}}
{{- $name := index . 0 -}}{{- $data := index . 1 -}}{{- $envAll := index . 2 -}}
{{- $from := $envAll.Values.endpoints.oslo_db.auth.admin.passwordFrom -}}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index $from $field) }}
{{- fail (printf "endpoints.oslo_db.auth.admin.passwordFrom.%s is required" $field) }}
{{- end }}
{{- end }}
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ $name }}
  annotations:
    # Deleted by helm WITH the release, after its pre-delete hooks (#254).
    krateo.io/teardown: with-release
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: SecretStore
    name: {{ $from.store | quote }}
  target:
    name: {{ $name }}
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      data:
{{- range $k, $v := $data }}
        {{ $k }}: {{ $v | quote }}
{{- end }}
  data:
    - secretKey: password
      remoteRef:
        key: {{ $from.secret | quote }}
        property: {{ $from.key | quote }}
{{- end -}}
