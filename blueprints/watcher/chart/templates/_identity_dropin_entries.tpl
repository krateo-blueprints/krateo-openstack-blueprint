{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where watcher reads service users' passwords in
watcher.conf, as {user (endpoints.identity.auth key), section, option}. With a user's passwordFrom each is stripped from
.Values.conf.watcher and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret watcher-identity).
*/}}
{{- define "watcher.identity_dropin_entries" -}}
{{- list (dict "user" "watcher" "section" "keystone_authtoken" "option" "password") (dict "user" "watcher" "section" "watcher_clients_auth" "option" "password") | toJson -}}
{{- end -}}
