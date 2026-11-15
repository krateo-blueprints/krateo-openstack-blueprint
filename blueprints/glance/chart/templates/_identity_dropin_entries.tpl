{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where glance reads service users' passwords in
glance-api.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.glance and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret glance-identity).
*/}}
{{- define "glance.identity_dropin_entries" -}}
{{- list (dict "user" "glance" "section" "keystone_authtoken" "option" "password") | toJson -}}
{{- end -}}
