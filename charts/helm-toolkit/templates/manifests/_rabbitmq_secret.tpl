{{/*
Krateo blueprint addition (issue #228): one RabbitMQ user's connection Secret, whose password comes
from values (OpenStack-Helm's behaviour) or, when the umbrella passes a reference, from a Secret that
External Secrets Operator generated once for the whole install.

abstract: |
  Renders the Secret <secretName> with RABBITMQ_CONNECTION (and, when asked, TRANSPORT_URL) for
  endpoints.oslo_messaging.auth.<userClass>, exactly as OpenStack-Helm's templates/secret-rabbitmq.yaml
  does. When endpoints.oslo_messaging.auth.<userClass>.passwordFrom names a SecretStore, a Secret and a
  key, it renders an ExternalSecret instead: same target name, same keys, the URLs built from values
  with the password read through that store. Every `<user>:<password>@` credential segment of a URL is
  replaced by `<user>:{{ .password }}@`: RABBITMQ_CONNECTION has exactly one, TRANSPORT_URL has one
  per RabbitMQ host. A generated password is alphanumeric, so URL quoting leaves it unchanged.
values: |
  endpoints:
    oslo_messaging:
      auth:
        admin:
          passwordFrom:
            store: openstack-credentials
            secret: openstack-credential-oslo-messaging-admin
            key: password
usage: |
  {{- tuple "admin" .Values.secrets.oslo_messaging.admin . (dict "protocol" "http") | include "helm-toolkit.manifests.rabbitmq_secret" }}
  The 4th element keeps a chart's own variant of the Secret:
    protocol     the RABBITMQ_CONNECTION port name, http or https (required: each chart decides it);
    transport    also render TRANSPORT_URL (default false);
    annotations  false to render no custom-secret annotations (default true).

creationPolicy Owner and krateo.io/teardown: with-release, for the reasons on
helm-toolkit.manifests.keystone_user_secret (#228, #254).
*/}}

{{- define "helm-toolkit.manifests.rabbitmq_secret" -}}
{{- $userClass := index . 0 -}}
{{- $secretName := index . 1 -}}
{{- $envAll := index . 2 -}}
{{- $opts := index . 3 | default dict -}}
{{- if not $opts.protocol }}
{{- fail "helm-toolkit.manifests.rabbitmq_secret: the 4th element must set protocol (http or https)" }}
{{- end }}
{{- $annotate := true -}}
{{- if hasKey $opts "annotations" }}{{- $annotate = index $opts "annotations" -}}{{- end }}
{{- $user := index $envAll.Values.endpoints.oslo_messaging.auth $userClass -}}
{{- $urls := dict "RABBITMQ_CONNECTION" (tuple "oslo_messaging" "internal" $userClass $opts.protocol $envAll | include "helm-toolkit.endpoints.authenticated_endpoint_uri_lookup") -}}
{{- if $opts.transport }}
{{- $_ := set $urls "TRANSPORT_URL" (tuple "oslo_messaging" "internal" $userClass "amqp" $envAll | include "helm-toolkit.endpoints.authenticated_transport_endpoint_uri_lookup") -}}
{{- end }}
{{- $from := $user.passwordFrom | default dict -}}
{{- if $from }}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index $from $field) }}
{{- fail (printf "endpoints.oslo_messaging.auth.%s.passwordFrom.%s is required: passwordFrom names the SecretStore, the Secret and the key the password is read from" $userClass $field) }}
{{- end }}
{{- end }}
{{- $literal := printf "%s:%s@" $user.username ($user.password | toString | urlquery) -}}
{{- $templated := dict -}}
{{- range $k, $url := $urls }}
{{- $n := sub (len (splitList $literal $url)) 1 -}}
{{- if or (lt $n 1) (and (eq $k "RABBITMQ_CONNECTION") (ne $n 1)) }}
{{- fail (printf "cannot place the password in the oslo_messaging %s %s: found %d %q segment(s)" $userClass $k $n "<user>:<password>@") }}
{{- end }}
{{- $_ := set $templated $k (replace $literal (printf "%s:{{ .password }}@" $user.username) $url) -}}
{{- end }}
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ $secretName }}
  annotations:
    # Deleted by helm WITH the release, after the pre-delete hooks that read this Secret (#254).
    krateo.io/teardown: with-release
{{- if $annotate }}
{{ tuple "oslo_messaging" $userClass $envAll | include "helm-toolkit.snippets.custom_secret_annotations" | indent 4 }}
{{- end }}
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
{{- range $k, $v := $templated }}
        {{ $k }}: {{ $v | quote }}
{{- end }}
  data:
    - secretKey: password
      remoteRef:
        key: {{ $from.secret | quote }}
        property: {{ $from.key | quote }}
{{- else }}
apiVersion: v1
kind: Secret
metadata:
  name: {{ $secretName }}
{{- if $annotate }}
  annotations:
{{ tuple "oslo_messaging" $userClass $envAll | include "helm-toolkit.snippets.custom_secret_annotations" | indent 4 }}
{{- end }}
type: Opaque
data:
{{- range $k, $v := $urls }}
  {{ $k }}: {{ $v | b64enc }}
{{- end }}
{{- end }}
{{- end -}}
