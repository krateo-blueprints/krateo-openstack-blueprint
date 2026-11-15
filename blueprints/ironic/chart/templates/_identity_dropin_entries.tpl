{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where ironic reads service users' passwords in
ironic.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.ironic and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret ironic-identity).
*/}}
{{- define "ironic.identity_dropin_entries" -}}
{{- list (dict "user" "glance" "section" "glance" "option" "password") (dict "user" "ironic" "section" "inspector" "option" "password") (dict "user" "ironic" "section" "keystone_authtoken" "option" "password") (dict "user" "ironic" "section" "neutron" "option" "password") (dict "user" "ironic" "section" "service_catalog" "option" "password") (dict "user" "ironic" "section" "service_user" "option" "password") (dict "user" "ironic" "section" "swift" "option" "password") | toJson -}}
{{- end -}}
