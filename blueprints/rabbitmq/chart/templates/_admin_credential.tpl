{{/*
The RabbitMQ administrator's and guest's passwords (Krateo blueprint addition, issue #228).

Without endpoints.oslo_messaging.auth.{user,guest}.passwordFrom: OpenStack-Helm's Secret
<deployment>-admin-user from values. With them: an ExternalSecret projecting the roots ESO generated
for the install under the NEW name <deployment>-admin-user-generated. Either way the Secret also holds
the admin connection URL, which the cluster-wait Job and the test Pod read by secretKeyRef: upstream
carries it, password included, as a literal env value in their specs.

The new name is the rotation mechanism. The rabbitmq-password init container writes both users and
their password hashes into the definitions file the server loads at boot
(management.load_definitions), which needs no old password; rotating means the server restarting
with the new values. Pointing the StatefulSet's secretKeyRef at a new name changes its pod spec -- it
rolls exactly at the switch -- and the pod can only ever read the generated values: it waits for ESO
to write them instead of racing helm's deletion of the old Secret.

Both users live in one definitions file, written by one init container from one Secret, so
passwordFrom is set on both or on neither.
*/}}

{{- define "rabbitmq.admin_from" -}}
{{- $auth := .Values.endpoints.oslo_messaging.auth -}}
{{- $user := ($auth.user.passwordFrom | default dict).secret -}}
{{- $guest := ($auth.guest.passwordFrom | default dict).secret -}}
{{- if and $user (not $guest) }}
{{- fail "endpoints.oslo_messaging.auth.user.passwordFrom is set but auth.guest.passwordFrom is not: both users are written into one definitions file from one Secret, so set both or neither" }}
{{- end }}
{{- if and $guest (not $user) }}
{{- fail "endpoints.oslo_messaging.auth.guest.passwordFrom is set but auth.user.passwordFrom is not: both users are written into one definitions file from one Secret, so set both or neither" }}
{{- end }}
{{- if $user }}true{{ end -}}
{{- end -}}

{{- define "rabbitmq.admin_user_secret_name" -}}
{{- if include "rabbitmq.admin_from" . }}{{ printf "%s-admin-user-generated" .deployment_name }}{{ else }}{{ printf "%s-admin-user" .deployment_name }}{{ end -}}
{{- end -}}

{{/* The admin connection URL's port name, as the cluster-wait Job and the test Pod use it. */}}
{{- define "rabbitmq.admin_protocol" -}}
{{- if .Values.manifests.certificates }}https{{ else }}http{{ end -}}
{{- end -}}

{{- define "rabbitmq.admin_connection" -}}
{{- tuple "oslo_messaging" "internal" "user" (include "rabbitmq.admin_protocol" .) . | include "helm-toolkit.endpoints.authenticated_endpoint_uri_lookup" -}}
{{- end -}}

{{/* RABBITMQ_ADMIN_CONNECTION for a container's env, from the admin-user Secret. */}}
{{- define "rabbitmq.admin_connection_env" -}}
- name: RABBITMQ_ADMIN_CONNECTION
  valueFrom:
    secretKeyRef:
      name: {{ include "rabbitmq.admin_user_secret_name" . | quote }}
      key: RABBITMQ_ADMIN_CONNECTION
{{- end -}}

{{- define "rabbitmq.admin_externalsecret" -}}
{{- $envAll := . -}}
{{- $auth := $envAll.Values.endpoints.oslo_messaging.auth -}}
{{- range $u := list "user" "guest" }}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index (index $auth $u).passwordFrom $field) }}
{{- fail (printf "endpoints.oslo_messaging.auth.%s.passwordFrom.%s is required" $u $field) }}
{{- end }}
{{- end }}
{{- end }}
{{- if ne $auth.user.passwordFrom.store $auth.guest.passwordFrom.store }}
{{- fail "endpoints.oslo_messaging.auth.user.passwordFrom.store and auth.guest.passwordFrom.store differ: one ExternalSecret reads both, through one SecretStore" }}
{{- end }}
{{- $connection := include "rabbitmq.admin_connection" $envAll -}}
{{- $literal := printf "://%s:%s@" $auth.user.username ($auth.user.password | toString | urlquery) -}}
{{- if ne (len (splitList $literal $connection)) 2 }}
{{- fail (printf "cannot place the admin password in the RabbitMQ admin connection URL: expected exactly one %q" "://<user>:<password>@") }}
{{- end }}
{{- $name := include "rabbitmq.admin_user_secret_name" $envAll }}
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
    name: {{ $auth.user.passwordFrom.store | quote }}
  target:
    name: {{ $name | quote }}
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      data:
        RABBITMQ_ADMIN_USERNAME: {{ $auth.user.username | quote }}
        RABBITMQ_ADMIN_PASSWORD: {{ "{{ .admin }}" | quote }}
        RABBITMQ_GUEST_PASSWORD: {{ "{{ .guest }}" | quote }}
        RABBITMQ_ADMIN_CONNECTION: {{ replace $literal (printf "://%s:{{ .admin }}@" $auth.user.username) $connection | quote }}
  data:
    - secretKey: admin
      remoteRef:
        key: {{ $auth.user.passwordFrom.secret | quote }}
        property: {{ $auth.user.passwordFrom.key | quote }}
    - secretKey: guest
      remoteRef:
        key: {{ $auth.guest.passwordFrom.secret | quote }}
        property: {{ $auth.guest.passwordFrom.key | quote }}
{{- end -}}
