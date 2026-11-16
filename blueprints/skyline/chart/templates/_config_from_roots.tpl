{{/*
Krateo (#228): skyline.yaml carries two passwords, the skyline Keystone user's (openstack.system_user_password) and the
skyline MariaDB user's (inside default.database_url). With either one's passwordFrom, skyline.yaml is rendered by the
ExternalSecret skyline-config (templates/configmap-etc.yaml) instead of the Secret skyline-etc, each password read from
its own root, and every reader mounts it from there.
*/}}
{{- define "skyline.config_from_roots" -}}
{{- if or ((.Values.endpoints.identity.auth.skyline.passwordFrom | default dict).secret) (include "helm-toolkit.utils.db_from_root" (tuple . "oslo_db" "skyline")) }}true{{- end -}}
{{- end -}}
