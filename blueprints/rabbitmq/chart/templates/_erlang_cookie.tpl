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
splits mid-roll. So the cookie only ever changes on a single node, in two phases (rabbitmq.erlang_cookie_mode,
from the live StatefulSet that rabbitmq.erlang_cookie_live looks up):

  live StatefulSet             wanted replicas   render
  none, or on the new cookie   any               normal: the generated cookie
  one pod, old cookie          1                 rotate: the generated cookie (that pod restarts once)
  more pods, old cookie        1                 hold: replicas 1 and the OLD cookie, still rendered;
                                                 the next reconcile, with one pod left, rotates
  old cookie                   more than 1       refused: set pod.replicas.server: 1 for the upgrade

0.3.57 refused whenever more than one pod ran, so setting replicas to 1 could never take effect: the
refused render was the one that would have scaled the StatefulSet down (measured, #228). "One pod"
means the live spec AND status: a pod still terminating counts, so the last node restarts only after
its peer has stopped, and it boots as the node that stopped last.
*/}}

{{- define "rabbitmq.erlang_cookie_from" -}}
{{- with .Values.endpoints.oslo_messaging.auth.erlang_cookie_from }}{{ if .secret }}true{{ end }}{{ end -}}
{{- end -}}

{{- define "rabbitmq.erlang_cookie_values_name" -}}{{ printf "%s-erlang-cookie" .deployment_name }}{{- end -}}
{{- define "rabbitmq.erlang_cookie_generated_name" -}}{{ printf "%s-erlang-cookie-generated" .deployment_name }}{{- end -}}

{{/* The live StatefulSet as JSON ({} when there is none). The only lookup: hack/validate-credentials.py
     replaces this expression with stand-in objects. */}}
{{- define "rabbitmq.erlang_cookie_live" -}}
{{- lookup "apps/v1" "StatefulSet" .Release.Namespace (printf "%s-rabbitmq" .deployment_name) | toJson -}}
{{- end -}}

{{/* "" (normal), "hold", or fails (refused). Only meaningful with erlang_cookie_from. */}}
{{- define "rabbitmq.erlang_cookie_mode" -}}
{{- $envAll := . -}}
{{- $live := include "rabbitmq.erlang_cookie_live" $envAll | fromJson -}}
{{- if and $live (include "rabbitmq.erlang_cookie_from" $envAll) }}
{{- $want := include "rabbitmq.erlang_cookie_generated_name" $envAll -}}
{{- $mounted := "" -}}
{{- range $v := ((($live.spec | default dict).template | default dict).spec | default dict).volumes | default list }}
{{- if eq $v.name "rabbitmq-erlang-cookie" }}{{- $mounted = ($v.secret | default dict).secretName | default "" -}}{{- end }}
{{- end }}
{{- if and $mounted (ne $mounted $want) }}
{{- $specReplicas := int (($live.spec | default dict).replicas | default 1) -}}
{{- $pods := int (($live.status | default dict).replicas | default 0) -}}
{{- $wanted := int $envAll.Values.pod.replicas.server -}}
{{- if gt $wanted 1 }}
{{- fail (printf "\n\nRabbitMQ's Erlang cookie changes on this upgrade (Secret %s -> %s), and every node of a cluster must hold the same cookie. The StatefulSet rolls one pod at a time, so with %d replica(s) running and %d wanted the first restarted node would be refused by its peers and the cluster would split. Set pod.replicas.server: 1 for this upgrade (componentValues.rabbitmq): the chart scales to one node on the old cookie, then rotates it. Set it back afterwards: new nodes join with the new cookie.\n" $mounted $want (max $specReplicas $pods) $wanted) }}
{{- else if or (gt $specReplicas 1) (gt $pods 1) }}hold{{ end }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "rabbitmq.erlang_cookie_secret_name" -}}
{{- if and (include "rabbitmq.erlang_cookie_from" .) (ne (include "rabbitmq.erlang_cookie_mode" .) "hold") }}{{ include "rabbitmq.erlang_cookie_generated_name" . }}{{ else }}{{ include "rabbitmq.erlang_cookie_values_name" . }}{{ end -}}
{{- end -}}

{{- define "rabbitmq.erlang_cookie_externalsecret" -}}
{{- $envAll := . -}}
{{- $from := $envAll.Values.endpoints.oslo_messaging.auth.erlang_cookie_from -}}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index $from $field) }}
{{- fail (printf "endpoints.oslo_messaging.auth.erlang_cookie_from.%s is required" $field) }}
{{- end }}
{{- end }}
{{- $name := include "rabbitmq.erlang_cookie_generated_name" $envAll }}
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
