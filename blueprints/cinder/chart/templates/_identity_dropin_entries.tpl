{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where cinder reads service users' passwords in
cinder.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.cinder and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret cinder-identity).
*/}}
{{- define "cinder.identity_dropin_entries" -}}
{{- list (dict "user" "swift" "section" "DEFAULT" "option" "backup_swift_key") | toJson -}}
{{- end -}}
