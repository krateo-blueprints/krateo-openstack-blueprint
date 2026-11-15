{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where mistral reads service users' passwords in
mistral.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.mistral and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret mistral-identity).
*/}}
{{- define "mistral.identity_dropin_entries" -}}
{{- list (dict "user" "mistral" "section" "keystone_authtoken" "option" "password") | toJson -}}
{{- end -}}
