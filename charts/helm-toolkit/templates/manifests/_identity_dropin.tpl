{{/*
Krateo blueprint addition (issue #228, service users slice 2b): the service users' passwords a chart reads
from its main oslo.config file, delivered as ONE drop-in per chart.

The chart lists what it reads as entries {user, section, option}: `user` is the endpoints.identity.auth key,
(section, option) where its password sits in .Values.conf.<confKey> (`password` mostly; also e.g.
`os_privileged_user_password`, `stack_domain_admin_password`, `backup_swift_key`). For every entry whose user
has a passwordFrom reference:
  - helm-toolkit.utils.identity_dropin_strip deletes (section, option) from .Values.conf.<confKey>, so the
    published value renders nowhere. Call it after the chart has filled its defaults into conf, right before
    the file is rendered;
  - helm-toolkit.manifests.identity_dropin renders ExternalSecret <service>-identity holding one file,
    85-identity.conf: each section once, each option = the password of its user's root (one data entry per
    user);
  - workloads read it from /etc/<project>/<project>.conf.d/ (oslo.config reads that directory even with
    --config-file), through helm-toolkit.snippets.identity_dropin_etc_source (pod.etcSources) or the
    mount/volume snippets.
With no referenced user, all of these render nothing and change nothing.

entries: a list of dicts, e.g. (list (dict "user" "aodh" "section" "keystone_authtoken" "option" "password")),
optionally with "conf" naming the .Values.conf key of another file the same program family reads. One
drop-in file serves every reader: each program registers only its own groups and ignores the others.
*/}}

{{- define "helm-toolkit.utils.identity_dropin_file" -}}85-identity.conf{{- end -}}

{{/* (tuple $envAll $entries): the entries whose user has a passwordFrom reference, as a JSON list. */}}
{{- define "helm-toolkit.utils.identity_dropin_active" -}}
{{- $envAll := index . 0 -}}{{- $entries := index . 1 -}}
{{- $out := list -}}
{{- range $e := $entries }}
{{- $u := index $envAll.Values.endpoints.identity.auth $e.user | default dict -}}
{{- if ($u.passwordFrom | default dict).secret }}{{- $out = append $out $e -}}{{- end -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{/* (dict "envAll" $ "entries" $entries "confKey" "aodh"): delete every referenced (section, option) from
     .Values.conf.<confKey>, or from .Values.conf.<entry.conf> for an entry naming another file's conf (masakari's
     monitors read the same user from masakarimonitors.conf). */}}
{{- define "helm-toolkit.utils.identity_dropin_strip" -}}
{{- $envAll := .envAll -}}{{- $confKey := .confKey -}}
{{- range $e := (include "helm-toolkit.utils.identity_dropin_active" (tuple .envAll .entries) | fromJsonArray) }}
{{- $conf := index $envAll.Values.conf ($e.conf | default $confKey) | default dict -}}
{{- $opts := index $conf $e.section -}}
{{- if kindIs "map" $opts }}{{- $_ := unset $opts $e.option -}}{{- end -}}
{{- end -}}
{{- end -}}

{{/* (dict "envAll" $ "entries" $entries "service" "aodh"): the ExternalSecret <service>-identity. */}}
{{- define "helm-toolkit.manifests.identity_dropin" -}}
{{- $envAll := .envAll -}}
{{- $active := include "helm-toolkit.utils.identity_dropin_active" (tuple $envAll .entries) | fromJsonArray -}}
{{- if $active }}
{{- $name := printf "%s-identity" .service -}}
{{- $sections := dict -}}
{{- $users := dict -}}
{{- range $e := $active }}
{{- $from := (index $envAll.Values.endpoints.identity.auth $e.user).passwordFrom -}}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index $from $field) }}
{{- fail (printf "endpoints.identity.auth.%s.passwordFrom needs %s (store, secret and key)" $e.user $field) }}
{{- end }}
{{- end }}
{{- $lines := index $sections $e.section | default list -}}
{{- $_ := set $sections $e.section (append $lines (printf "%s = {{ .pw_%s }}" $e.option $e.user)) -}}
{{- $_ := set $users $e.user $from -}}
{{- end }}
{{- $text := "" -}}
{{- range $section := keys $sections | sortAlpha }}
{{- $text = printf "%s[%s]\n%s\n" $text $section (index $sections $section | join "\n") -}}
{{- end }}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ $name }}
  annotations:
    # Deleted by helm WITH the release, after the pre-delete hooks (#254).
    krateo.io/teardown: with-release
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: SecretStore
    name: {{ (index $users (keys $users | sortAlpha | first)).store | quote }}
  target:
    name: {{ $name }}
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      data:
        {{ include "helm-toolkit.utils.identity_dropin_file" $envAll }}: {{ $text | quote }}
  data:
{{- range $user := keys $users | sortAlpha }}
{{- $from := index $users $user }}
    - secretKey: {{ printf "pw_%s" $user }}
      remoteRef:
        key: {{ $from.secret | quote }}
        property: {{ $from.key | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{/* (tuple $envAll $entries $service): a pod.etcSources entry projecting the drop-in, as JSON, or "". */}}
{{- define "helm-toolkit.snippets.identity_dropin_etc_source" -}}
{{- $envAll := index . 0 -}}{{- $entries := index . 1 -}}{{- $service := index . 2 -}}
{{- if (include "helm-toolkit.utils.identity_dropin_active" (tuple $envAll $entries) | fromJsonArray) -}}
{{- dict "secret" (dict "name" (printf "%s-identity" $service)) | toJson -}}
{{- end -}}
{{- end -}}

{{/* (tuple $envAll $entries $service [$dir]): a volumeMount of the drop-in file into <dir> (default
     /etc/<service>/<service>.conf.d), for workloads without pod.etcSources. Indent at the call site. */}}
{{- define "helm-toolkit.snippets.identity_dropin_mount" -}}
{{- $envAll := index . 0 -}}{{- $entries := index . 1 -}}{{- $service := index . 2 -}}
{{- $dir := printf "/etc/%s/%s.conf.d" $service $service -}}{{- if gt (len .) 3 }}{{- $dir = index . 3 -}}{{- end -}}
{{- if (include "helm-toolkit.utils.identity_dropin_active" (tuple $envAll $entries) | fromJsonArray) }}
- name: {{ printf "%s-identity" $service }}
  mountPath: {{ printf "%s/%s" $dir (include "helm-toolkit.utils.identity_dropin_file" $envAll) }}
  subPath: {{ include "helm-toolkit.utils.identity_dropin_file" $envAll }}
  readOnly: true
{{- end }}
{{- end -}}

{{- define "helm-toolkit.snippets.identity_dropin_volume" -}}
{{- $envAll := index . 0 -}}{{- $entries := index . 1 -}}{{- $service := index . 2 -}}
{{- if (include "helm-toolkit.utils.identity_dropin_active" (tuple $envAll $entries) | fromJsonArray) }}
- name: {{ printf "%s-identity" $service }}
  secret:
    secretName: {{ printf "%s-identity" $service }}
    defaultMode: 0444
{{- end }}
{{- end -}}
