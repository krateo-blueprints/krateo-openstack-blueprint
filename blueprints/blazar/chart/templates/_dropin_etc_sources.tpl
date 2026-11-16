{{/* Krateo (#228): every drop-in source this chart's workloads use, as a JSON list in pod.etcSources form: the admin
     password, the memcache key and (0.3.67) the blazar user's password for the physical-host plugin. Every container
     that mounts blazar.conf receives these, Jobs included (0.3.65's ceilometer-db-sync, given none, failed its
     post-upgrade hook). Each entry renders only when its secret is generated, so with no reference the list is empty
     and nothing changes. */}}
{{- define "blazar.dropin_etc_sources" -}}
{{- $out := list -}}
{{- if .Values.endpoints.identity.auth.admin.passwordFrom }}{{- $out = append $out (dict "secret" (dict "name" "blazar-admin-password-etc")) }}{{- end }}
{{- with include "helm-toolkit.snippets.memcache_secret_key_etc_source" (tuple . "blazar") }}{{- $out = append $out (fromJson .) }}{{- end }}
{{- if .Values.endpoints.identity.auth.blazar.passwordFrom }}{{- $out = append $out (dict "secret" (dict "name" "blazar-physical-host-password-etc")) }}{{- end }}
{{- $out | toJson -}}
{{- end -}}
