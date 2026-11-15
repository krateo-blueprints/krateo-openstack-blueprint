{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where heat reads service users' passwords in
heat.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.heat and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret heat-identity).
*/}}
{{- define "heat.identity_dropin_entries" -}}
{{- list (dict "user" "heat_stack_user" "section" "DEFAULT" "option" "stack_domain_admin_password") (dict "user" "heat" "section" "keystone_authtoken" "option" "password") (dict "user" "heat_trustee" "section" "trustee" "option" "password") | toJson -}}
{{- end -}}
