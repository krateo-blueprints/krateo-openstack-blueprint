{{/* Krateo (#228, per-service MariaDB users): the database URLs neutron's main config carries, as entries of
     helm-toolkit.manifests.db_dropin (charts/helm-toolkit/templates/manifests/_db_dropin.tpl). With the user's
     passwordFrom each is stripped from the config and rendered in the drop-in neutron-db-connection instead.
     `tls` is the condition under which configmap-etc.yaml appends the MariaDB TLS query string to that URL,
     so the drop-in carries the same URL. */}}
{{- define "neutron.db_dropin_entries" -}}
{{- $out := list -}}
{{- $out = append $out (dict "endpoint" "oslo_db" "user" "neutron" "section" "database" "option" "connection" "tls" .Values.manifests.certificates) -}}
{{- $out | toJson -}}
{{- end -}}
