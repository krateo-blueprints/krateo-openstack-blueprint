{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where gnocchi reads service users' passwords in
gnocchi.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.gnocchi and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret gnocchi-identity).
*/}}
{{- define "gnocchi.identity_dropin_entries" -}}
{{- list (dict "user" "gnocchi" "section" "keystone_authtoken" "option" "password") | toJson -}}
{{- end -}}
