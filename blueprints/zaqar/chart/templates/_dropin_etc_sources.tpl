{{/* Krateo (#228). zaqar's signed-URL key reference (0.3.67), as JSON ({} when unset): the umbrella generates it
     per install instead of the published SOMELONGSECRETKEY, and it reaches zaqar.conf.d as the drop-in
     zaqar-signed-url-secret-key (secret-signed-url-secret-key.yaml). */}}
{{- define "zaqar.signed_url_key_ref" -}}
{{- (((.Values.endpoints.messaging | default dict).auth | default dict).signed_url_secret_keyFrom | default dict) | toJson -}}
{{- end -}}

{{/* Krateo (#228): every drop-in source this chart's workloads use, as a JSON list in pod.etcSources form. Every
     container that mounts zaqar.conf receives these, Jobs included (0.3.65's ceilometer-db-sync, given none, failed
     its post-upgrade hook). Each entry renders only when its secret is generated, so with no reference the list is
     empty and nothing changes. */}}
{{- define "zaqar.dropin_etc_sources" -}}
{{- $out := list -}}
{{- with include "helm-toolkit.snippets.memcache_secret_key_etc_source" (tuple . "zaqar") }}{{- $out = append $out (fromJson .) }}{{- end }}
{{- with include "helm-toolkit.snippets.secret_conf_dropin_etc_source" (dict "from" (include "zaqar.signed_url_key_ref" . | fromJson) "name" "zaqar-signed-url-secret-key") }}{{- $out = append $out (fromJson .) }}{{- end }}
{{- with include "helm-toolkit.snippets.db_dropin_etc_source" (tuple . (include "zaqar.db_dropin_entries" . | fromJsonArray) "zaqar") }}{{- $out = append $out (fromJson .) }}{{- end }}
{{- $out | toJson -}}
{{- end -}}
