{{/* Krateo (#228, per-service RabbitMQ users): the RabbitMQ URLs manila's main config carries, as entries of
     helm-toolkit.manifests.transport_dropin (charts/helm-toolkit/templates/manifests/_transport_dropin.tpl), each
     built by helm-toolkit.endpoints.authenticated_transport_endpoint_uri_lookup. With the user's passwordFrom each is stripped
     from the config and rendered in the drop-in manila-transport instead. */}}
{{- define "manila.transport_dropin_entries" -}}
{{- $out := list -}}
{{- $out = append $out (dict "user" "manila" "section" "DEFAULT" "option" "transport_url" "lookup" "transport") -}}
{{- $out | toJson -}}
{{- end -}}
