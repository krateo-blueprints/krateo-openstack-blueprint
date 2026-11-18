{{/*
Krateo blueprint addition (issue #228): one database user's DB_CONNECTION Secret, whose password comes
from values (OpenStack-Helm's behaviour) or, when the umbrella passes a reference, from a Secret that
External Secrets Operator generated once for the whole install.

abstract: |
  Renders the Secret <secretName> with DB_CONNECTION for endpoints.oslo_db.auth.<userClass>, exactly as
  OpenStack-Helm's templates/secret-db.yaml does (the TLS query string when manifests.certificates).
  When endpoints.oslo_db.auth.<userClass>.passwordFrom names a SecretStore, a Secret and a key, it
  renders an ExternalSecret instead: same target name, same key, the URL built from values with the
  password read through that store. The URL is rendered as usual and exactly its `://<user>:<password>@`
  part is replaced by `://<user>:{{ .password | urlquery }}@` (helm-toolkit.utils.db_url_with_password):
  URL-quoted in the ExternalSecret as values mode quotes it (authenticated_endpoint_uri_lookup).
values: |
  endpoints:
    oslo_db:
      auth:
        admin:
          passwordFrom:
            store: openstack-credentials
            secret: openstack-credential-oslo-db-admin
            key: password
usage: |
  {{- tuple "admin" .Values.secrets.oslo_db.admin . | include "helm-toolkit.manifests.db_connection_secret" }}
  An optional 4th element keeps a chart's own variant of the Secret:
    tls           whether DB_CONNECTION carries the TLS query string (default: manifests.certificates);
    password_key  also render the bare password under this key (e.g. DB_PASSWORD);
    annotations   false to render no custom-secret annotations (default true);
    endpoint      the oslo_db family the user belongs to (default oslo_db; nova's oslo_db_api and
                  oslo_db_cell0, octavia's oslo_db_persistence). Its TLS query string is the same.
  {{- tuple "admin" .Values.secrets.oslo_db.admin . (dict "tls" .Values.tls.oslo_db) | include "helm-toolkit.manifests.db_connection_secret" }}

creationPolicy Owner and krateo.io/teardown: with-release, for the reasons on
helm-toolkit.manifests.keystone_user_secret (#228, #254).
*/}}

{{- define "helm-toolkit.manifests.db_connection_secret" -}}
{{- $userClass := index . 0 -}}
{{- $secretName := index . 1 -}}
{{- $envAll := index . 2 -}}
{{- $opts := dict -}}
{{- if gt (len .) 3 }}{{- $opts = index . 3 | default dict -}}{{- end }}
{{- $tls := $envAll.Values.manifests.certificates -}}
{{- if hasKey $opts "tls" }}{{- $tls = index $opts "tls" -}}{{- end }}
{{- $annotate := true -}}
{{- if hasKey $opts "annotations" }}{{- $annotate = index $opts "annotations" -}}{{- end }}
{{- $endpoint := index $opts "endpoint" | default "oslo_db" -}}
{{- $user := index (index $envAll.Values.endpoints $endpoint).auth $userClass -}}
{{- $connection := tuple $endpoint "internal" $userClass "mysql" $envAll | include "helm-toolkit.endpoints.authenticated_endpoint_uri_lookup" -}}
{{- if $tls -}}
{{- $connection = printf "%s?charset=utf8&ssl_ca=/etc/mysql/certs/ca.crt&ssl_key=/etc/mysql/certs/tls.key&ssl_cert=/etc/mysql/certs/tls.crt&ssl_verify_cert" $connection -}}
{{- end -}}
{{- $from := $user.passwordFrom | default dict -}}
{{- if $from }}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index $from $field) }}
{{- fail (printf "endpoints.%s.auth.%s.passwordFrom.%s is required: passwordFrom names the SecretStore, the Secret and the key the password is read from" $endpoint $userClass $field) }}
{{- end }}
{{- end }}
{{- $templated := include "helm-toolkit.utils.db_url_with_password" (tuple $connection $user.username $user.password "{{ .password | urlquery }}" (printf "%s %s connection" $endpoint $userClass)) }}
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ $secretName }}
  annotations:
    # Deleted by helm WITH the release, after the pre-delete hooks that read this Secret (#254).
    krateo.io/teardown: with-release
{{- if $annotate }}
{{ tuple $endpoint $userClass $envAll | include "helm-toolkit.snippets.custom_secret_annotations" | indent 4 }}
{{- end }}
spec:
  # OnChange (#309 C): refreshed only when this ExternalSecret itself changes (a new root, a new
  # template), never on a timer, so a root edited by hand (which ESO regenerates) reaches no live
  # credential until a rotation; the umbrella reports the edit (status.credentialDrift).
  refreshPolicy: OnChange
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
        DB_CONNECTION: {{ $templated | quote }}
{{- if $opts.password_key }}
        {{ $opts.password_key }}: {{ "{{ .password }}" | quote }}
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
{{ tuple $endpoint $userClass $envAll | include "helm-toolkit.snippets.custom_secret_annotations" | indent 4 }}
{{- end }}
type: Opaque
data:
  DB_CONNECTION: {{ $connection | b64enc }}
{{- if $opts.password_key }}
  {{ $opts.password_key }}: {{ $user.password | toString | b64enc }}
{{- end }}
{{- end }}
{{- end -}}
