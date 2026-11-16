{{/* Krateo (#228, per-service MariaDB users): the database URLs cinder's main config carries, as entries of
     helm-toolkit.manifests.db_dropin (charts/helm-toolkit/templates/manifests/_db_dropin.tpl). With the user's
     passwordFrom each is stripped from the config and rendered in the drop-in cinder-db-connection instead.
     `tls` is the condition under which configmap-etc.yaml appends the MariaDB TLS query string to that URL,
     so the drop-in carries the same URL. */}}
{{- define "cinder.db_dropin_entries" -}}
{{- $out := list -}}
{{- $out = append $out (dict "endpoint" "oslo_db" "user" "cinder" "section" "database" "option" "connection" "tls" (.Values.tls.oslo_db | default false)) -}}
{{- $out | toJson -}}
{{- end -}}
