{{/* Krateo (#228, per-service MariaDB users): the database URLs keystone's main config carries, as entries of
     helm-toolkit.manifests.db_dropin (charts/helm-toolkit/templates/manifests/_db_dropin.tpl). With the user's
     passwordFrom each is stripped from the config and rendered in the drop-in keystone-db-connection instead.
     `tls` is the condition under which configmap-etc.yaml appends the MariaDB TLS query string to that URL,
     so the drop-in carries the same URL. */}}
{{- define "keystone.db_dropin_entries" -}}
{{- $out := list -}}
{{- $out = append $out (dict "endpoint" "oslo_db" "user" "keystone" "section" "database" "option" "connection" "tls" (and .Values.manifests.certificates .Values.endpoints.oslo_db.auth.admin.secret.tls.internal)) -}}
{{- $out | toJson -}}
{{- end -}}
