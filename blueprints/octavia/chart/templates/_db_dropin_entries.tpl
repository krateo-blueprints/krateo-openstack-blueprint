{{/* Krateo (#228, per-service MariaDB users): the database URLs octavia's main config carries, as entries of
     helm-toolkit.manifests.db_dropin (charts/helm-toolkit/templates/manifests/_db_dropin.tpl). With the user's
     passwordFrom each is stripped from the config and rendered in the drop-in octavia-db-connection instead. */}}
{{- define "octavia.db_dropin_entries" -}}
{{- $out := list -}}
{{- $out = append $out (dict "endpoint" "oslo_db" "user" "octavia" "section" "database" "option" "connection" "tls" false) -}}
{{- $out = append $out (dict "endpoint" "oslo_db_persistence" "user" "octavia" "section" "task_flow" "option" "persistence_connection" "tls" false) -}}
{{- $out | toJson -}}
{{- end -}}
