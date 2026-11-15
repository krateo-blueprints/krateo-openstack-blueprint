{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where octavia reads service users' passwords in
octavia.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.octavia and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret octavia-identity).
*/}}
{{- define "octavia.identity_dropin_entries" -}}
{{- list (dict "user" "octavia" "section" "keystone_authtoken" "option" "password") | toJson -}}
{{- end -}}
