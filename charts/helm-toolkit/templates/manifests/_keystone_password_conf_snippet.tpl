{{/*
Krateo blueprint addition (issue #228): a Keystone user's password as an oslo.config drop-in, for the
few services that read a password from their config file rather than from an openrc Secret.

abstract: |
  When endpoints.identity.auth.<userClass>.passwordFrom is set, renders an ExternalSecret whose target
  Secret <secretName> holds one key, <fileName>, with the content

    [<section>]
    <option> = <the password>

  read through the named SecretStore. The chart mounts that Secret into the service's <project>.conf.d
  directory (oslo.config reads it after --config-file, so it overrides the main file) and stops
  rendering the option into the main config. Renders nothing when passwordFrom is not set.
usage: |
  {{- tuple . "admin" "blazar-admin-password-etc" "90-admin-password.conf" "DEFAULT" "os_admin_password" | include "helm-toolkit.manifests.keystone_password_conf_snippet" }}
*/}}

{{- define "helm-toolkit.manifests.keystone_password_conf_snippet" -}}
{{- $envAll := index . 0 -}}
{{- $userClass := index . 1 -}}
{{- $secretName := index . 2 -}}
{{- $fileName := index . 3 -}}
{{- $section := index . 4 -}}
{{- $option := index . 5 -}}
{{- $from := (index $envAll.Values.endpoints.identity.auth $userClass).passwordFrom | default dict -}}
{{- if $from }}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ $secretName }}
  annotations:
    # Deleted with the release, after its pre-delete hooks, not finalized ahead of it (#254).
    krateo.io/teardown: with-release
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: SecretStore
    name: {{ $from.store | quote }}
  target:
    name: {{ $secretName }}
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      data:
        {{ $fileName }}: {{ printf "[%s]\n%s = {{ .password }}\n" $section $option | quote }}
  data:
    - secretKey: password
      remoteRef:
        key: {{ $from.secret | quote }}
        property: {{ $from.key | quote }}
{{- end }}
{{- end -}}
