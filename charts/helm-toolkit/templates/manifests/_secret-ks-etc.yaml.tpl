{{/*
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/}}

{{- define "helm-toolkit.manifests.secret_ks_etc" -}}
{{- $envAll := index . "envAll" -}}
{{- $serviceName := index . "serviceName" -}}
{{- $serviceUserSections := index . "serviceUserSections" -}}
{{- $serviceNamePretty := $serviceName | replace "_" "-" -}}
{{- /* Krateo (#228): with endpoints.identity.auth.<user>.passwordFrom on any of these users, the Secret
       becomes an ExternalSecret of the same name: each section file is rendered exactly as below, with
       that user's password replaced by the ESO template action for its root ({{ .pw_<user> }}). Users
       without a reference keep their value from values. */ -}}
{{- $refs := dict -}}
{{- range $epName, $sectionName := $serviceUserSections }}
{{- $from := (index $envAll.Values.endpoints.identity.auth $epName).passwordFrom | default dict -}}
{{- if $from.secret }}{{- $_ := set $refs $epName $from -}}{{- end }}
{{- end }}
{{- if $refs }}
{{- $store := "" -}}
{{- range $epName, $from := $refs }}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index $from $field) }}
{{- fail (printf "endpoints.identity.auth.%s.passwordFrom.%s is required" $epName $field) }}
{{- end }}
{{- end }}
{{- if and $store (ne $store $from.store) }}
{{- fail (printf "%s-ks-etc: every passwordFrom must name the same SecretStore (one ExternalSecret reads them all)" $serviceNamePretty) }}
{{- end }}
{{- $store = $from.store -}}
{{- end }}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ printf "%s-ks-etc" $serviceNamePretty | quote }}
  annotations:
    {{ tuple $envAll | include "helm-toolkit.snippets.release_uuid" }}
    # Deleted by helm WITH the release, after the pre-delete hooks (#254).
    krateo.io/teardown: with-release
{{ tuple "ks_etc" $serviceName $envAll | include "helm-toolkit.snippets.custom_secret_annotations" | indent 4 }}
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: SecretStore
    name: {{ $store | quote }}
  target:
    name: {{ printf "%s-ks-etc" $serviceNamePretty | quote }}
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      data:
{{- range $epName, $sectionName := $serviceUserSections }}
{{- $epAuth := index $envAll.Values.endpoints.identity.auth $epName -}}
{{- $password := $epAuth.password | toString -}}
{{- if hasKey $refs $epName }}{{- $password = printf "{{ .pw_%s }}" $epName -}}
{{- else if contains "{{" $password }}
{{- fail (printf "endpoints.identity.auth.%s.password contains '{{', which the ExternalSecret template would execute" $epName) }}
{{- end }}
{{- $configSection := dict
  "region_name" $epAuth.region_name
  "project_name" $epAuth.project_name
  "project_domain_name" $epAuth.project_domain_name
  "user_domain_name" $epAuth.user_domain_name
  "username" $epAuth.username
  "password" $password
-}}
{{- $configSnippet := dict $sectionName $configSection }}
        {{ printf "%s_%s.conf" $serviceName $sectionName }}: {{ include "helm-toolkit.utils.to_oslo_conf" $configSnippet | quote }}
{{- end }}
  data:
{{- range $epName, $from := $refs }}
    - secretKey: {{ printf "pw_%s" $epName }}
      remoteRef:
        key: {{ $from.secret | quote }}
        property: {{ $from.key | quote }}
{{- end }}
{{- else }}
---
apiVersion: v1
kind: Secret
metadata:
  name: {{ printf "%s-ks-etc" $serviceNamePretty | quote }}
  annotations:
    {{ tuple $envAll | include "helm-toolkit.snippets.release_uuid" }}
{{ tuple "ks_etc" $serviceName $envAll | include "helm-toolkit.snippets.custom_secret_annotations" | indent 4 }}
type: Opaque
data:
{{- range $epName, $sectionName := $serviceUserSections }}
{{- $epAuth := index $envAll.Values.endpoints.identity.auth $epName -}}
{{- $configSection := dict
  "region_name" $epAuth.region_name
  "project_name" $epAuth.project_name
  "project_domain_name" $epAuth.project_domain_name
  "user_domain_name" $epAuth.user_domain_name
  "username" $epAuth.username
  "password" $epAuth.password
-}}
{{- $configSnippet := dict $sectionName $configSection }}
{{ printf "%s_%s.conf" $serviceName $sectionName | indent 2 }}: {{ include "helm-toolkit.utils.to_oslo_conf" $configSnippet | b64enc }}
{{- end }}
{{- end }}
{{- end -}}
