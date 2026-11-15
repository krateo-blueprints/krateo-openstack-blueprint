{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where tacker reads service users' passwords in
tacker.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.tacker and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret tacker-identity).
*/}}
{{- define "tacker.identity_dropin_entries" -}}
{{- list (dict "user" "tacker" "section" "alarm_auth" "option" "password") (dict "user" "tacker" "section" "keystone_authtoken" "option" "password") | toJson -}}
{{- end -}}
