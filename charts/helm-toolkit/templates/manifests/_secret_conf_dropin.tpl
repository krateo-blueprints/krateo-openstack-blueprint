{{/*
Krateo blueprint addition (issue #228): one generated secret option delivered as an oslo.config drop-in.

For a secret that lives in a service's rendered config (an option, not a password in a URL), with a
reference {store, secret, key} the umbrella passes:
  - helm-toolkit.utils.secret_option_strip removes the option from every section of the given
    .Values.conf.<key> dicts, so the published value renders nowhere;
  - helm-toolkit.manifests.secret_conf_dropin renders the ExternalSecret <name>, one file
    <file> = [<section>] <option> = <generated>;
  - workloads read it from /etc/<project>/<project>.conf.d/ (oslo.config reads that directory even
    when --config-file is given; verified with oslo.config 10.8.1) through
    helm-toolkit.snippets.secret_conf_dropin_etc_source appended to pod.etcSources.
Without the reference all three render nothing and change nothing.

dict arguments:
  envAll   the chart's root context
  from     the reference: {store, secret, key} (empty or missing = values mode)
  name     the ExternalSecret and target Secret name
  file     the drop-in file name (e.g. 81-metadata-proxy.conf)
  section  the oslo.config section
  option   the option name
*/}}

{{- define "helm-toolkit.utils.secret_option_from" -}}
{{- with . }}{{ if .secret }}true{{ end }}{{ end -}}
{{- end -}}

{{/* (dict "envAll" $ "from" $ref "option" "opt" "confKeys" (list "nova")): delete <option> from every
     section of each .Values.conf.<confKey>. */}}
{{- define "helm-toolkit.utils.secret_option_strip" -}}
{{- if include "helm-toolkit.utils.secret_option_from" .from }}
{{- $option := .option -}}
{{- range $confKey := .confKeys }}
{{- range $section, $opts := (index $.envAll.Values.conf $confKey | default dict) }}
{{- if kindIs "map" $opts }}{{- $_ := unset $opts $option -}}{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "helm-toolkit.manifests.secret_conf_dropin" -}}
{{- if include "helm-toolkit.utils.secret_option_from" .from }}
{{- $from := .from -}}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index $from $field) }}
{{- fail (printf "the reference for %s needs %s (store, secret and key)" $.name $field) }}
{{- end }}
{{- end }}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ .name }}
  annotations:
    # Deleted by helm WITH the release, after the pre-delete hooks (#254).
    krateo.io/teardown: with-release
spec:
  # OnChange (#309 C): refreshed only when this ExternalSecret itself changes (a new root, a new
  # template), never on a timer, so a root edited by hand (which ESO regenerates) reaches no live
  # credential until a rotation; the umbrella reports the edit (status.credentialDrift).
  refreshPolicy: OnChange
  secretStoreRef:
    kind: SecretStore
    name: {{ $from.store | quote }}
  target:
    name: {{ .name }}
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      data:
        {{ .file }}: {{ printf "[%s]\n%s = {{ .password }}\n" .section .option | quote }}
  data:
    - secretKey: password
      remoteRef:
        key: {{ $from.secret | quote }}
        property: {{ $from.key | quote }}
{{- end }}
{{- end -}}

{{/* (dict "from" $ref "name" "<secret>"): a pod.etcSources entry as JSON, or "". */}}
{{- define "helm-toolkit.snippets.secret_conf_dropin_etc_source" -}}
{{- if include "helm-toolkit.utils.secret_option_from" .from -}}
{{- dict "secret" (dict "name" .name) | toJson -}}
{{- end -}}
{{- end -}}

{{/* The metadata proxy shared secret's reference (#228), shared by nova and neutron, as JSON ({} when unset). */}}
{{- define "helm-toolkit.utils.metadata_proxy_secret_ref" -}}
{{- (((.Values.endpoints.compute_metadata | default dict).auth | default dict).metadata_proxy_shared_secretFrom | default dict) | toJson -}}
{{- end -}}

{{/* (dict "envAll" $ "from" $ref "option" "password" "confKey" "cyborg" "sections" (list "nova")): with the
     reference, delete <option> from exactly these sections of .Values.conf.<confKey> (a service user's
     password where the same file holds other users' passwords that are not rotated by this reference). */}}
{{- define "helm-toolkit.utils.secret_option_strip_sections" -}}
{{- if include "helm-toolkit.utils.secret_option_from" .from }}
{{- $conf := index .envAll.Values.conf .confKey | default dict -}}
{{- $option := .option -}}
{{- range $section := .sections }}
{{- $opts := index $conf $section -}}
{{- if kindIs "map" $opts }}{{- $_ := unset $opts $option -}}{{- end }}
{{- end }}
{{- end }}
{{- end -}}

{{/* (dict "from" $ref "name" "<secret>" "file" "<file>" "dir" "/etc/<p>/<p>.conf.d"): a volumeMount of the
     drop-in file, for charts without pod.etcSources. Indent at the call site. */}}
{{- define "helm-toolkit.snippets.secret_conf_dropin_mount" -}}
{{- if include "helm-toolkit.utils.secret_option_from" .from }}
- name: {{ .name }}
  mountPath: {{ printf "%s/%s" .dir .file }}
  subPath: {{ .file }}
  readOnly: true
{{- end }}
{{- end -}}

{{- define "helm-toolkit.snippets.secret_conf_dropin_volume" -}}
{{- if include "helm-toolkit.utils.secret_option_from" .from }}
- name: {{ .name }}
  secret:
    secretName: {{ .name }}
    defaultMode: 0444
{{- end }}
{{- end -}}
