{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where ceilometer reads service users' passwords in
ceilometer.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.ceilometer and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret ceilometer-identity).
*/}}
{{- define "ceilometer.identity_dropin_entries" -}}
{{- list (dict "user" "ceilometer" "section" "service_credentials" "option" "password") | toJson -}}
{{- end -}}
