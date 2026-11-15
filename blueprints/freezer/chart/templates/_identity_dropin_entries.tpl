{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where freezer reads service users' passwords in
freezer.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.freezer and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret freezer-identity).
*/}}
{{- define "freezer.identity_dropin_entries" -}}
{{- list (dict "user" "freezer" "section" "keystone_authtoken" "option" "password") | toJson -}}
{{- end -}}
