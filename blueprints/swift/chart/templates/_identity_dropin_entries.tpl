{{/*
Krateo blueprint addition (issue #228, service users slice 2b): where swift's proxy reads the swift user's password
(proxy-server.conf [filter:authtoken]), as {user, section, option}. With its passwordFrom it is stripped from
.Values.conf.proxy_server and delivered by helm-toolkit.manifests.identity_dropin (ExternalSecret swift-identity)
into /etc/swift/proxy-server.conf.d/, which the proxy then loads as a directory.
*/}}
{{- define "swift.identity_dropin_entries" -}}
{{- list (dict "user" "swift" "section" "filter:authtoken" "option" "password") | toJson -}}
{{- end -}}

{{/* "true" when the proxy loads /etc/swift/proxy-server.conf.d (a drop-in exists: memcache key or identity). */}}
{{- define "swift.proxy_conf_dir" -}}
{{- if or (include "helm-toolkit.utils.memcache_secret_key_from" .) (include "helm-toolkit.utils.identity_dropin_active" (tuple . (include "swift.identity_dropin_entries" . | fromJsonArray)) | fromJsonArray) -}}true{{- end -}}
{{- end -}}
