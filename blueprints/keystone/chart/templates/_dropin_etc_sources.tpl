{{/* Krateo (#228): every drop-in source keystone's Jobs use, as a JSON list in pod.etcSources form: the database
     URL drop-in (keystone-db-connection) when the keystone MariaDB user's password is generated. Every container
     that mounts keystone.conf receives it; with no reference the list is empty and nothing changes. */}}
{{- define "keystone.dropin_etc_sources" -}}
{{- $out := list -}}
{{- with include "helm-toolkit.snippets.db_dropin_etc_source" (tuple . (include "keystone.db_dropin_entries" . | fromJsonArray) "keystone") }}{{- $out = append $out (fromJson .) }}{{- end }}
{{- $out | toJson -}}
{{- end -}}
