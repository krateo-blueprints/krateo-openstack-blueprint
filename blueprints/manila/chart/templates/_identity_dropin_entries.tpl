{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where manila reads service users' passwords in
manila.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.manila and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret manila-identity).
*/}}
{{- define "manila.identity_dropin_entries" -}}
{{- list (dict "user" "manila" "section" "cinder" "option" "password") (dict "user" "manila" "section" "glance" "option" "password") (dict "user" "manila" "section" "keystone_authtoken" "option" "password") (dict "user" "manila" "section" "neutron" "option" "password") (dict "user" "manila" "section" "nova" "option" "password") | toJson -}}
{{- end -}}
