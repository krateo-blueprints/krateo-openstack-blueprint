{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where cyborg reads service users' passwords in
cyborg.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.cyborg and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret cyborg-identity).
*/}}
{{- define "cyborg.identity_dropin_entries" -}}
{{- list (dict "user" "cyborg" "section" "keystone_authtoken" "option" "password") (dict "user" "cyborg" "section" "service_catalog" "option" "password") | toJson -}}
{{- end -}}
