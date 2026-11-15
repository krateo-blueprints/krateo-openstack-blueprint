{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where cloudkitty reads service users' passwords in
cloudkitty.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.cloudkitty and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret cloudkitty-identity).
*/}}
{{- define "cloudkitty.identity_dropin_entries" -}}
{{- list (dict "user" "cloudkitty" "section" "DEFAULT" "option" "os_privileged_user_password") | toJson -}}
{{- end -}}
