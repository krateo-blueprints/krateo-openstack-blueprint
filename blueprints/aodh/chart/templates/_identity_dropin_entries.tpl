{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where aodh reads service users' passwords in
aodh.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.aodh and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret aodh-identity).
*/}}
{{- define "aodh.identity_dropin_entries" -}}
{{- list (dict "user" "aodh" "section" "keystone_authtoken" "option" "password") (dict "user" "aodh" "section" "service_credentials" "option" "password") | toJson -}}
{{- end -}}
