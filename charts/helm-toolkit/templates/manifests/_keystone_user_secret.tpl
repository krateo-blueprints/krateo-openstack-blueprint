{{/*
Krateo blueprint addition (issue #228): one Keystone user's openrc Secret, whose password comes from
values (OpenStack-Helm's behaviour) or, when the umbrella passes a reference, from a Secret that
External Secrets Operator generated once for the whole install.

abstract: |
  Renders the Secret <secretName> with the OS_* keys of endpoints.identity.auth.<userClass>, exactly
  as helm-toolkit.snippets.keystone_secret_openrc does. When
  endpoints.identity.auth.<userClass>.passwordFrom names a SecretStore, a Secret and a key, it renders
  an ExternalSecret instead: same target name, same keys, the non-secret keys from values and
  OS_PASSWORD read through that store. Consumers read the same Secret name and keys either way.
values: |
  endpoints:
    identity:
      auth:
        admin:
          passwordFrom:
            store: openstack-credentials
            secret: openstack-credential-identity-admin
            key: password
usage: |
  {{- tuple "admin" .Values.secrets.identity.admin . | include "helm-toolkit.manifests.keystone_user_secret" }}
  A chart whose openrc Secret carries extra non-secret keys passes them as a 4th element:
  {{- tuple "admin" .Values.secrets.identity.admin . (dict "OS_AUTH_TYPE" "password") | include "helm-toolkit.manifests.keystone_user_secret" }}

Why an ExternalSecret and not a Secret built at render time: the chart renders on every reconcile, so
a generated value would change every time, and a lookup of the generated Secret would put it in the
release. Why creationPolicy Owner: measured on ESO 2.14.0 (#228), a helm upgrade from the Secret to
this ExternalSecret leaves the name absent for ~13 ms (helm deletes, ESO creates), a rollback across
that boundary fails without changing anything, and the next upgrade succeeds; Orphan loses the Secret
on that retry.
*/}}

{{- define "helm-toolkit.manifests.keystone_user_secret" -}}
{{- $userClass := index . 0 -}}
{{- $secretName := index . 1 -}}
{{- $envAll := index . 2 -}}
{{- $extra := dict -}}
{{- if gt (len .) 3 }}{{- $extra = index . 3 | default dict -}}{{- end }}
{{- $user := index $envAll.Values.endpoints.identity.auth $userClass -}}
{{- $from := $user.passwordFrom | default dict -}}
{{- if $from }}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index $from $field) }}
{{- fail (printf "endpoints.identity.auth.%s.passwordFrom.%s is required: passwordFrom names the SecretStore, the Secret and the key the password is read from" $userClass $field) }}
{{- end }}
{{- end }}
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ $secretName }}
  annotations:
    # Deleted by helm WITH the release, after the pre-delete hooks that read this Secret -- not
    # finalized ahead of the uninstall by composition-dynamic-controller, which would take the Secret
    # with it (#254; core-provider's opt-out contract: exactly this value).
    krateo.io/teardown: with-release
{{ tuple "identity" $userClass $envAll | include "helm-toolkit.snippets.custom_secret_annotations" | indent 4 }}
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
        OS_AUTH_URL: {{ tuple "identity" "internal" "api" $envAll | include "helm-toolkit.endpoints.keystone_endpoint_uri_lookup" | quote }}
        OS_REGION_NAME: {{ $user.region_name | quote }}
        OS_INTERFACE: {{ $user.interface | default "internal" | quote }}
        OS_PROJECT_DOMAIN_NAME: {{ $user.project_domain_name | quote }}
        OS_PROJECT_NAME: {{ $user.project_name | quote }}
        OS_USER_DOMAIN_NAME: {{ $user.user_domain_name | quote }}
        OS_USERNAME: {{ $user.username | quote }}
        OS_PASSWORD: {{ "{{ .password }}" | quote }}
        OS_DEFAULT_DOMAIN: {{ $user.default_domain_id | default "default" | quote }}
{{- if $user.cacert }}
        OS_CACERT: {{ $user.cacert | quote }}
{{- end }}
{{- range $k, $v := $extra }}
        {{ $k }}: {{ $v | toString | quote }}
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
  annotations:
{{ tuple "identity" $userClass $envAll | include "helm-toolkit.snippets.custom_secret_annotations" | indent 4 }}
type: Opaque
data:
{{- tuple $userClass "internal" $envAll | include "helm-toolkit.snippets.keystone_secret_openrc" | indent 2 -}}
{{- range $k, $v := $extra }}
  {{ $k }}: {{ $v | toString | b64enc }}
{{- end }}
{{- end }}
{{- end -}}
