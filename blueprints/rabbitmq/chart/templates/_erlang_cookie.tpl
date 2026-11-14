{{/*
The Erlang distribution cookie (Krateo blueprint addition, issue #228).

The cookie is the only credential of Erlang distribution (port 25672, reachable from any pod: the
Service is headless and the network policy admits all ingress). Whoever holds it gets a remote shell
on the node, i.e. runs code in the RabbitMQ pod. OpenStack-Helm publishes its default,
`openstack-cookie`.

Without endpoints.oslo_messaging.auth.erlang_cookie_from: OpenStack-Helm's Secret
<deployment>-erlang-cookie from values. With it: an ExternalSecret projecting the root ESO generated
for the install under the NEW name <deployment>-erlang-cookie-generated. A new name changes the
StatefulSet's pod spec, so the node restarts exactly at the switch and only ever reads the generated
value (the same mechanism as the admin password, _admin_credential.tpl). Either way the server reads
RABBITMQ_ERLANG_COOKIE from that Secret: upstream carries the cookie as a literal env value in the
pod spec.

Every node of a cluster must hold the same cookie. A StatefulSet rolls one pod at a time, so rotating
the cookie under more than one replica would start a node that its peers refuse, and the cluster
splits mid-roll. The render therefore refuses that one case -- a live StatefulSet with more than one
replica still mounting another cookie Secret -- and says how to proceed. The decision is
rabbitmq.erlang_cookie_rotation_check, which takes the live object as an argument so it can be
tested without a cluster.
*/}}

{{- define "rabbitmq.erlang_cookie_from" -}}
{{- with .Values.endpoints.oslo_messaging.auth.erlang_cookie_from }}{{ if .secret }}true{{ end }}{{ end -}}
{{- end -}}

{{- define "rabbitmq.erlang_cookie_secret_name" -}}
{{- if include "rabbitmq.erlang_cookie_from" . }}{{ printf "%s-erlang-cookie-generated" .deployment_name }}{{ else }}{{ printf "%s-erlang-cookie" .deployment_name }}{{ end -}}
{{- end -}}

{{/* (list $envAll $live): fails when applying this render to the live StatefulSet $live (a dict, empty
     when there is none) would roll a new cookie through more than one replica. */}}
{{- define "rabbitmq.erlang_cookie_rotation_check" -}}
{{- $envAll := index . 0 -}}{{- $live := index . 1 | default dict -}}
{{- if $live }}
{{- $want := include "rabbitmq.erlang_cookie_secret_name" $envAll -}}
{{- $mounted := "" -}}
{{- range $v := ((($live.spec | default dict).template | default dict).spec | default dict).volumes | default list }}
{{- if eq $v.name "rabbitmq-erlang-cookie" }}{{- $mounted = ($v.secret | default dict).secretName | default "" -}}{{- end }}
{{- end }}
{{- $replicas := int (($live.spec | default dict).replicas | default 1) -}}
{{- $wanted := int $envAll.Values.pod.replicas.server -}}
{{- if and $mounted (ne $mounted $want) (or (gt $replicas 1) (gt $wanted 1)) }}
{{- fail (printf "\n\nRabbitMQ's Erlang cookie changes on this upgrade (Secret %s -> %s), and every node of a cluster must hold the same cookie. The StatefulSet rolls one pod at a time, so with %d replica(s) running and %d wanted the first restarted node would be refused by its peers and the cluster would split. Set pod.replicas.server: 1 for this upgrade (componentValues.rabbitmq), let it complete, then set it back: new nodes join with the new cookie.\n" $mounted $want $replicas $wanted) }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "rabbitmq.erlang_cookie_externalsecret" -}}
{{- $envAll := . -}}
{{- $from := $envAll.Values.endpoints.oslo_messaging.auth.erlang_cookie_from -}}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index $from $field) }}
{{- fail (printf "endpoints.oslo_messaging.auth.erlang_cookie_from.%s is required" $field) }}
{{- end }}
{{- end }}
{{- $name := include "rabbitmq.erlang_cookie_secret_name" $envAll }}
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ $name | quote }}
  annotations:
    # Deleted by helm WITH the release, after its pre-delete hooks (#254).
    krateo.io/teardown: with-release
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: SecretStore
    name: {{ $from.store | quote }}
  target:
    name: {{ $name | quote }}
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      data:
        erlang_cookie: {{ "{{ .password }}" | quote }}
  data:
    - secretKey: password
      remoteRef:
        key: {{ $from.secret | quote }}
        property: {{ $from.key | quote }}
{{- end -}}
