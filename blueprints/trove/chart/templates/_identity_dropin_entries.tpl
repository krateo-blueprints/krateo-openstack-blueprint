{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where trove reads service users' passwords in
trove.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.trove and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret trove-identity).
*/}}
{{- define "trove.identity_dropin_entries" -}}
{{- list (dict "user" "trove" "section" "keystone_authtoken" "option" "password") (dict "user" "trove" "section" "service_credentials" "option" "password") | toJson -}}
{{- end -}}
