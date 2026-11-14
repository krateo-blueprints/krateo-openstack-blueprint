{{/*
Krateo blueprint addition (issue #228): the keystonemiddleware memcache secret key, generated per
service instead of OpenStack-Helm's published value.

keystonemiddleware caches token-validation results in memcached, which has no authentication and is
reachable from any pod. With memcache_security_strategy ENCRYPT the entries are encrypted and
authenticated with memcache_secret_key; with a published key, anyone reaching memcached can forge a
"this token is valid" entry for the service.

With endpoints.oslo_cache.auth.memcache_secret_keyFrom (store, secret, key: set by the umbrella, one
root per service), the key is read through External Secrets Operator and delivered as a config drop-in:
  - helm-toolkit.utils.memcache_secret_key_strip removes memcache_secret_key from every section of
    .Values.conf.<service> (call it in configmap-etc.yaml after the upstream defaults are set), so the
    published value is rendered nowhere;
  - helm-toolkit.manifests.memcache_secret_key_etc renders the ExternalSecret <service>-memcache-secret-key,
    one file: [<section>] memcache_secret_key = <generated>;
  - the workloads read it from /etc/<service>/<service>.conf.d/ (oslo.config's default config dir)
    through helm-toolkit.snippets.memcache_secret_key_etc_source (charts with pod.etcSources) or the
    _mount/_volume snippets (charts without).
Without the reference all four render nothing and change nothing.
*/}}

{{- define "helm-toolkit.utils.memcache_secret_key_from" -}}
{{- with ((.Values.endpoints.oslo_cache | default dict).auth | default dict).memcache_secret_keyFrom }}{{ if .secret }}true{{ end }}{{ end -}}
{{- end -}}

{{/* (tuple $envAll $service): with the reference, delete memcache_secret_key from every section of
     .Values.conf.<service>. */}}
{{- define "helm-toolkit.utils.memcache_secret_key_strip" -}}
{{- $envAll := index . 0 -}}{{- $service := index . 1 -}}
{{- if include "helm-toolkit.utils.memcache_secret_key_from" $envAll }}
{{- range $section, $opts := (index $envAll.Values.conf $service) }}
{{- if kindIs "map" $opts }}{{- $_ := unset $opts "memcache_secret_key" -}}{{- end }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "helm-toolkit.utils.memcache_secret_key_file" -}}80-memcache-secret-key.conf{{- end -}}

{{/* (tuple $envAll $service [$section]): the ExternalSecret <service>-memcache-secret-key. */}}
{{- define "helm-toolkit.manifests.memcache_secret_key_etc" -}}
{{- $envAll := index . 0 -}}{{- $service := index . 1 -}}
{{- $section := "keystone_authtoken" -}}{{- if gt (len .) 2 }}{{- $section = index . 2 -}}{{- end -}}
{{- if include "helm-toolkit.utils.memcache_secret_key_from" $envAll }}
{{- $from := $envAll.Values.endpoints.oslo_cache.auth.memcache_secret_keyFrom -}}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index $from $field) }}
{{- fail (printf "endpoints.oslo_cache.auth.memcache_secret_keyFrom.%s is required" $field) }}
{{- end }}
{{- end }}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ printf "%s-memcache-secret-key" $service }}
  annotations:
    # Deleted by helm WITH the release, after the pre-delete hooks (#254).
    krateo.io/teardown: with-release
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: SecretStore
    name: {{ $from.store | quote }}
  target:
    name: {{ printf "%s-memcache-secret-key" $service }}
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      data:
        {{ include "helm-toolkit.utils.memcache_secret_key_file" $envAll }}: {{ printf "[%s]\nmemcache_secret_key = {{ .password }}\n" $section | quote }}
  data:
    - secretKey: password
      remoteRef:
        key: {{ $from.secret | quote }}
        property: {{ $from.key | quote }}
{{- end }}
{{- end -}}

{{/* (tuple $envAll $service): a pod.etcSources entry projecting the drop-in, as JSON, or "". */}}
{{- define "helm-toolkit.snippets.memcache_secret_key_etc_source" -}}
{{- $envAll := index . 0 -}}{{- $service := index . 1 -}}
{{- if include "helm-toolkit.utils.memcache_secret_key_from" $envAll -}}
{{- dict "secret" (dict "name" (printf "%s-memcache-secret-key" $service)) | toJson -}}
{{- end -}}
{{- end -}}

{{/* (tuple $envAll $service [$dir]): a volumeMount of the drop-in file into <dir> (default
     /etc/<service>/<service>.conf.d), for charts without pod.etcSources. Indent at the call site. */}}
{{- define "helm-toolkit.snippets.memcache_secret_key_mount" -}}
{{- $envAll := index . 0 -}}{{- $service := index . 1 -}}
{{- $dir := printf "/etc/%s/%s.conf.d" $service $service -}}{{- if gt (len .) 2 }}{{- $dir = index . 2 -}}{{- end -}}
{{- if include "helm-toolkit.utils.memcache_secret_key_from" $envAll }}
- name: {{ printf "%s-memcache-secret-key" $service }}
  mountPath: {{ printf "%s/%s" $dir (include "helm-toolkit.utils.memcache_secret_key_file" $envAll) }}
  subPath: {{ include "helm-toolkit.utils.memcache_secret_key_file" $envAll }}
  readOnly: true
{{- end }}
{{- end -}}

{{- define "helm-toolkit.snippets.memcache_secret_key_volume" -}}
{{- $envAll := index . 0 -}}{{- $service := index . 1 -}}
{{- if include "helm-toolkit.utils.memcache_secret_key_from" $envAll }}
- name: {{ printf "%s-memcache-secret-key" $service }}
  secret:
    secretName: {{ printf "%s-memcache-secret-key" $service }}
    defaultMode: 0444
{{- end }}
{{- end -}}
