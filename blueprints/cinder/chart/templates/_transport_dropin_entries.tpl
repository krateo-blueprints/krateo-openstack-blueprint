{{/* Krateo (#228, per-service RabbitMQ users): the RabbitMQ URLs cinder's main config carries, as entries of
     helm-toolkit.manifests.transport_dropin (charts/helm-toolkit/templates/manifests/_transport_dropin.tpl), each
     built by helm-toolkit.endpoints.authenticated_transport_endpoint_uri_lookup. With the user's passwordFrom each is stripped
     from the config and rendered in the drop-in cinder-transport instead. */}}
{{- define "cinder.transport_dropin_entries" -}}
{{- $out := list -}}
{{- $out = append $out (dict "user" "cinder" "section" "DEFAULT" "option" "transport_url" "lookup" "transport") -}}
{{- $out | toJson -}}
{{- end -}}
