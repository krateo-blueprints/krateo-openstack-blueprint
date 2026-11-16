{{/* Krateo (#228, per-service MariaDB users): the database URLs gnocchi's main config carries, as entries of
     helm-toolkit.manifests.db_dropin (charts/helm-toolkit/templates/manifests/_db_dropin.tpl). With the user's
     passwordFrom each is stripped from the config and rendered in the drop-in gnocchi-db-connection instead. */}}
{{- define "gnocchi.db_dropin_entries" -}}
{{- $out := list -}}
{{- $out = append $out (dict "endpoint" "oslo_db" "user" "gnocchi" "section" "database" "option" "connection" "tls" false) -}}
{{- /* [indexer] url logs in as this user only with the MariaDB indexer; the PostgreSQL one is endpoints.oslo_db_postgresql */ -}}
{{- if ne (.Values.conf.gnocchi.indexer.driver | toString) "postgresql" }}
{{- $out = append $out (dict "endpoint" "oslo_db" "user" "gnocchi" "section" "indexer" "option" "url" "tls" false) -}}
{{- end }}
{{- $out | toJson -}}
{{- end -}}
