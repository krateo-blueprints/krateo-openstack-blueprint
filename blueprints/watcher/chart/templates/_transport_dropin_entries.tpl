{{/* Krateo (#228, per-service RabbitMQ users): the RabbitMQ URLs watcher's main config carries, as entries of
     helm-toolkit.manifests.transport_dropin (charts/helm-toolkit/templates/manifests/_transport_dropin.tpl), each
     built by helm-toolkit.endpoints.authenticated_endpoint_uri_lookup, as configmap-etc.yaml builds it. With the user's passwordFrom each is stripped
     from the config and rendered in the drop-in watcher-transport instead. */}}
{{- define "watcher.transport_dropin_entries" -}}
{{- $out := list -}}
{{- $out = append $out (dict "user" "watcher" "section" "DEFAULT" "option" "transport_url" "lookup" "endpoint") -}}
{{- $out | toJson -}}
{{- end -}}
