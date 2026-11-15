{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where barbican reads service users' passwords in
barbican.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.barbican and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret barbican-identity).
*/}}
{{- define "barbican.identity_dropin_entries" -}}
{{- list (dict "user" "barbican" "section" "keystone_authtoken" "option" "password") | toJson -}}
{{- end -}}
