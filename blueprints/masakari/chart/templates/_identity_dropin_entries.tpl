{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where masakari reads service users' passwords in
masakari.conf and its monitors' masakarimonitors.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.masakari and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret masakari-identity).
*/}}
{{- define "masakari.identity_dropin_entries" -}}
{{- list (dict "user" "masakari" "section" "api" "option" "password" "conf" "masakarimonitors") (dict "user" "masakari" "section" "DEFAULT" "option" "os_privileged_user_password") (dict "user" "masakari" "section" "keystone_authtoken" "option" "password") | toJson -}}
{{- end -}}
