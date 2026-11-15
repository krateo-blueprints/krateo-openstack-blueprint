{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where designate reads service users' passwords in
designate.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.designate and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret designate-identity).
*/}}
{{- define "designate.identity_dropin_entries" -}}
{{- list (dict "user" "designate" "section" "keystone_authtoken" "option" "password") | toJson -}}
{{- end -}}
