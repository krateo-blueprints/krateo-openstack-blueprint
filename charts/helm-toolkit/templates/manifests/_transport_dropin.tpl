{{/*
Krateo blueprint addition (issue #228, per-service RabbitMQ users, 0.3.69): the RabbitMQ URLs a chart's main
oslo.config file carries, built from each user's generated root and delivered as ONE drop-in per chart.

A per-service RabbitMQ user's password sits inside a URL ([DEFAULT] transport_url, [oslo_messaging_notifications]
transport_url). The chart lists every such option as an entry {user, section, option, lookup}: `user` its
endpoints.oslo_messaging.auth key, (section, option) where the URL sits in .Values.conf.<confKey> (or
.Values.conf.<entry.conf>), and `lookup` how the chart builds it: "transport" (the default,
helm-toolkit.endpoints.authenticated_transport_endpoint_uri_lookup, one `<user>:<password>@` per RabbitMQ host) or
"endpoint" (helm-toolkit.endpoints.authenticated_endpoint_uri_lookup, watcher's). For every entry whose user has a
passwordFrom reference:
  - helm-toolkit.utils.transport_dropin_refuse refuses a literal at (section, option). Call it BEFORE the chart fills
    its defaults: a URL written into values carries a password, and it would be silently replaced;
  - helm-toolkit.utils.transport_dropin_strip deletes (section, option), so the published password renders nowhere.
    Call it after the chart has filled its defaults, right before the file is rendered;
  - helm-toolkit.manifests.transport_dropin renders ExternalSecret <service>-transport, one file 88-transport.conf:
    each section once, each option = the URL exactly as the chart builds it, with every password segment read from
    the user's root and URL-quoted;
  - only the containers whose programs build an oslo.messaging transport from that config receive it
    (hack/mq-readers.yaml): helm-toolkit.snippets.transport_dropin_etc_source (pod.etcSources) or the mount/volume
    snippets. It is a separate Secret from <service>-db-connection because the two reach different containers (the
    agents use RabbitMQ and never the database; db-init and db-drop use the database and never RabbitMQ).
With no referenced user all of these render nothing and change nothing.

One RabbitMQ account has one password and one root. No chart names a username under two keys (measured; the map
hack/mq-users.yaml holds one key per chart and username, and hack/validate-credentials.py check 15c fails on a render it
cannot hold), and a username rendered by two charts gets one root passed to both.
*/}}

{{- define "helm-toolkit.utils.transport_dropin_file" -}}88-transport.conf{{- end -}}

{{/* (tuple $envAll $user): "true" when endpoints.oslo_messaging.auth.<user>.passwordFrom names a root. */}}
{{- define "helm-toolkit.utils.mq_from_root" -}}
{{- $envAll := index . 0 -}}{{- $user := index . 1 -}}
{{- $auth := ($envAll.Values.endpoints.oslo_messaging | default dict).auth | default dict -}}
{{- if ((index $auth $user | default dict).passwordFrom | default dict).secret }}true{{- end -}}
{{- end -}}

{{/* (tuple $envAll $entries): the entries whose user has a passwordFrom reference, as a JSON list. */}}
{{- define "helm-toolkit.utils.transport_dropin_active" -}}
{{- $envAll := index . 0 -}}{{- $entries := index . 1 -}}
{{- $out := list -}}
{{- range $e := $entries }}
{{- if include "helm-toolkit.utils.mq_from_root" (tuple $envAll $e.user) }}{{- $out = append $out $e -}}{{- end -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{/* (tuple $envAll $user $lookup): the URL user <user> connects with, as the chart builds it (see the header). */}}
{{- define "helm-toolkit.utils.transport_url" -}}
{{- $envAll := index . 0 -}}{{- $user := index . 1 -}}{{- $lookup := index . 2 | default "transport" -}}
{{- if eq $lookup "transport" -}}
{{- tuple "oslo_messaging" "internal" $user "amqp" $envAll | include "helm-toolkit.endpoints.authenticated_transport_endpoint_uri_lookup" -}}
{{- else if eq $lookup "endpoint" -}}
{{- tuple "oslo_messaging" "internal" $user "amqp" $envAll | include "helm-toolkit.endpoints.authenticated_endpoint_uri_lookup" -}}
{{- else -}}
{{- fail (printf "helm-toolkit.utils.transport_url: lookup %q is neither transport nor endpoint" $lookup) -}}
{{- end -}}
{{- end -}}

{{/* (tuple $url $username $password $placeholder $what): $url with every `<username>:<urlquoted password>@` (one per
     RabbitMQ host) replaced by `<username>:<placeholder>@`. Fails when the URL holds none. */}}
{{- define "helm-toolkit.utils.transport_url_with_password" -}}
{{- $url := index . 0 -}}{{- $username := index . 1 -}}{{- $password := index . 2 -}}{{- $placeholder := index . 3 -}}{{- $what := index . 4 -}}
{{- $literal := printf "%s:%s@" $username ($password | toString | urlquery) -}}
{{- if lt (len (splitList $literal $url)) 2 }}
{{- fail (printf "cannot place the password in the %s URL: no %q segment" $what "<user>:<password>@") }}
{{- end }}
{{- replace $literal (printf "%s:%s@" $username $placeholder) $url -}}
{{- end -}}

{{/* (dict "envAll" $ "entries" $entries "confKey" "aodh"): with a user's root, a literal at one of its options is
     refused. Call it before the chart fills its defaults. Renders nothing. */}}
{{- define "helm-toolkit.utils.transport_dropin_refuse" -}}
{{- $envAll := .envAll -}}{{- $confKey := .confKey -}}
{{- range $e := (include "helm-toolkit.utils.transport_dropin_active" (tuple .envAll .entries) | fromJsonArray) }}
{{- $k := $e.conf | default $confKey -}}
{{- $opts := index (index $envAll.Values.conf $k | default dict) $e.section | default dict -}}
{{- if not (empty (index $opts $e.option)) }}
{{- fail (printf "conf.%s.%s.%s is set while endpoints.oslo_messaging.auth.%s.passwordFrom is: the URL carries that RabbitMQ user's password, so it is built from the generated root (drop-in %s). Remove it, or set the broker through endpoints.oslo_messaging (#228)." $k $e.section $e.option $e.user (include "helm-toolkit.utils.transport_dropin_file" $envAll)) }}
{{- end }}
{{- end }}
{{- end -}}

{{/* (dict "envAll" $ "entries" $entries "confKey" "aodh"): delete every referenced (section, option) from
     .Values.conf.<confKey> (or .Values.conf.<entry.conf>). Call it after the chart has filled its defaults. */}}
{{- define "helm-toolkit.utils.transport_dropin_strip" -}}
{{- $envAll := .envAll -}}{{- $confKey := .confKey -}}
{{- range $e := (include "helm-toolkit.utils.transport_dropin_active" (tuple .envAll .entries) | fromJsonArray) }}
{{- $opts := index (index $envAll.Values.conf ($e.conf | default $confKey) | default dict) $e.section -}}
{{- if kindIs "map" $opts }}{{- $_ := unset $opts $e.option -}}{{- end -}}
{{- end -}}
{{- end -}}

{{/* (dict "envAll" $ "entries" $entries "service" "aodh"): the ExternalSecret <service>-transport. */}}
{{- define "helm-toolkit.manifests.transport_dropin" -}}
{{- $envAll := .envAll -}}
{{- $active := include "helm-toolkit.utils.transport_dropin_active" (tuple $envAll .entries) | fromJsonArray -}}
{{- if $active }}
{{- $name := printf "%s-transport" .service -}}
{{- $sections := dict -}}
{{- $keys := dict -}}
{{- $store := "" -}}
{{- range $e := $active }}
{{- $u := index $envAll.Values.endpoints.oslo_messaging.auth $e.user -}}
{{- $from := $u.passwordFrom -}}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index $from $field) }}
{{- fail (printf "endpoints.oslo_messaging.auth.%s.passwordFrom needs %s (store, secret and key)" $e.user $field) }}
{{- end }}
{{- end }}
{{- if and $store (ne $store $from.store) }}
{{- fail (printf "%s: every passwordFrom must name the same SecretStore (one ExternalSecret reads them all)" $name) }}
{{- end }}
{{- $store = $from.store -}}
{{- $key := printf "pw_%s" $e.user -}}
{{- $url := include "helm-toolkit.utils.transport_url" (tuple $envAll $e.user ($e.lookup | default "transport")) -}}
{{- $url = include "helm-toolkit.utils.transport_url_with_password" (tuple $url $u.username $u.password (printf "{{ .%s | urlquery }}" $key) (printf "oslo_messaging %s" $e.user)) -}}
{{- $lines := index $sections $e.section | default list -}}
{{- $_ := set $sections $e.section (append $lines (printf "%s = %s" $e.option $url)) -}}
{{- $_ := set $keys $key $from -}}
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
    name: {{ $store | quote }}
  target:
    name: {{ $name }}
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      data:
        {{ include "helm-toolkit.utils.transport_dropin_file" $envAll }}: {{ $text | quote }}
  data:
{{- range $key := keys $keys | sortAlpha }}
{{- $from := index $keys $key }}
    - secretKey: {{ $key }}
      remoteRef:
        key: {{ $from.secret | quote }}
        property: {{ $from.key | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{/* (tuple $envAll $entries $service): a pod.etcSources entry projecting the drop-in, as JSON, or "". */}}
{{- define "helm-toolkit.snippets.transport_dropin_etc_source" -}}
{{- $envAll := index . 0 -}}{{- $entries := index . 1 -}}{{- $service := index . 2 -}}
{{- if (include "helm-toolkit.utils.transport_dropin_active" (tuple $envAll $entries) | fromJsonArray) -}}
{{- dict "secret" (dict "name" (printf "%s-transport" $service)) | toJson -}}
{{- end -}}
{{- end -}}

{{/* (tuple $envAll $entries $service [$dir]): a volumeMount of the drop-in file into <dir> (default
     /etc/<service>/<service>.conf.d), for workloads without pod.etcSources. Indent at the call site. */}}
{{- define "helm-toolkit.snippets.transport_dropin_mount" -}}
{{- $envAll := index . 0 -}}{{- $entries := index . 1 -}}{{- $service := index . 2 -}}
{{- $dir := printf "/etc/%s/%s.conf.d" $service $service -}}{{- if gt (len .) 3 }}{{- $dir = index . 3 -}}{{- end -}}
{{- if (include "helm-toolkit.utils.transport_dropin_active" (tuple $envAll $entries) | fromJsonArray) }}
- name: {{ printf "%s-transport" $service }}
  mountPath: {{ printf "%s/%s" $dir (include "helm-toolkit.utils.transport_dropin_file" $envAll) }}
  subPath: {{ include "helm-toolkit.utils.transport_dropin_file" $envAll }}
  readOnly: true
{{- end }}
{{- end -}}

{{- define "helm-toolkit.snippets.transport_dropin_volume" -}}
{{- $envAll := index . 0 -}}{{- $entries := index . 1 -}}{{- $service := index . 2 -}}
{{- if (include "helm-toolkit.utils.transport_dropin_active" (tuple $envAll $entries) | fromJsonArray) }}
- name: {{ printf "%s-transport" $service }}
  secret:
    secretName: {{ printf "%s-transport" $service }}
    defaultMode: 0444
{{- end }}
{{- end -}}

{{/* (tuple $envAll $entries $dir): `--config-dir <dir>` as two exec-probe argv lines ("- --config-dir" and "- <dir>"),
     when the drop-in is active. A health probe that parses the main config with --config-file and no project (neutron's
     health-probe.py: cfg.CONF(sys.argv[1:])) never reads <service>.conf.d by itself, and without transport_url it
     reports success without checking anything. Indent at the call site. */}}
{{- define "helm-toolkit.snippets.transport_dropin_probe_args" -}}
{{- $envAll := index . 0 -}}{{- $entries := index . 1 -}}{{- $dir := index . 2 -}}
{{- if (include "helm-toolkit.utils.transport_dropin_active" (tuple $envAll $entries) | fromJsonArray) -}}
- --config-dir
- {{ $dir }}
{{- end -}}
{{- end -}}
