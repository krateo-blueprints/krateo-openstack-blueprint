{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where magnum reads service users' passwords in
magnum.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.magnum and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret magnum-identity).
*/}}
{{- define "magnum.identity_dropin_entries" -}}
{{- list (dict "user" "magnum" "section" "keystone_authtoken" "option" "password") (dict "user" "magnum_stack_user" "section" "trust" "option" "trustee_domain_admin_password") | toJson -}}
{{- end -}}
