{{/* Krateo (#228): every drop-in source keystone's Jobs use, as a JSON list in pod.etcSources form: the database
     URL drop-in (keystone-db-connection) when the keystone MariaDB user's password is generated. Every container
     that mounts keystone.conf receives it; with no reference the list is empty and nothing changes. */}}
{{- define "keystone.dropin_etc_sources" -}}
{{- $out := list -}}
{{- with include "helm-toolkit.snippets.db_dropin_etc_source" (tuple . (include "keystone.db_dropin_entries" . | fromJsonArray) "keystone") }}{{- $out = append $out (fromJson .) }}{{- end }}
{{- $out | toJson -}}
{{- end -}}

{{/* Krateo (#228, 0.3.69): the drop-ins of the db-sync Job, whose programs use RabbitMQ (hack/mq-readers.yaml: keystone-manage bootstrap, which db-sync.sh runs, emits audit notifications as it creates the domain and project when a notification driver is set): keystone.dropin_etc_sources plus keystone-transport, the RabbitMQ URLs. */}}
{{- define "keystone.db_sync_etc_sources" -}}
{{- $out := include "keystone.dropin_etc_sources" . | fromJsonArray -}}
{{- with include "helm-toolkit.snippets.transport_dropin_etc_source" (tuple . (include "keystone.transport_dropin_entries" . | fromJsonArray) "keystone") }}{{- $out = append $out (fromJson .) }}{{- end }}
{{- $out | toJson -}}
{{- end -}}
