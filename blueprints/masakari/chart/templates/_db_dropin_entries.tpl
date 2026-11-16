{{/* Krateo (#228, per-service MariaDB users): the database URLs masakari's main config carries, as entries of
     helm-toolkit.manifests.db_dropin (charts/helm-toolkit/templates/manifests/_db_dropin.tpl). With the user's
     passwordFrom each is stripped from the config and rendered in the drop-in masakari-db-connection instead.
     `tls` is the condition under which configmap-etc.yaml appends the MariaDB TLS query string to that URL,
     so the drop-in carries the same URL. */}}
{{- define "masakari.db_dropin_entries" -}}
{{- $out := list -}}
{{- $out = append $out (dict "endpoint" "oslo_db" "user" "masakari" "section" "database" "option" "connection" "tls" .Values.manifests.certificates) -}}
{{- $out = append $out (dict "endpoint" "oslo_db" "user" "masakari" "section" "taskflow" "option" "connection" "tls" .Values.manifests.certificates) -}}
{{- $out | toJson -}}
{{- end -}}
