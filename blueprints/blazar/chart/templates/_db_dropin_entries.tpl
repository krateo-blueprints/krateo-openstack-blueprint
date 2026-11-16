{{/* Krateo (#228, per-service MariaDB users): the database URLs blazar's main config carries, as entries of
     helm-toolkit.manifests.db_dropin (charts/helm-toolkit/templates/manifests/_db_dropin.tpl). With the user's
     passwordFrom each is stripped from the config and rendered in the drop-in blazar-db-connection instead. */}}
{{- define "blazar.db_dropin_entries" -}}
{{- $out := list -}}
{{- $out = append $out (dict "endpoint" "oslo_db" "user" "blazar" "section" "database" "option" "connection" "tls" false) -}}
{{- $out | toJson -}}
{{- end -}}
