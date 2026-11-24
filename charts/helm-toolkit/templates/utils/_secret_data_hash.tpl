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

{{/*
abstract: |
  A hash of what decides the DATA of the Secrets a template file renders, for a pod template that must roll when a credential
  changes and only then (#449). helm-toolkit.utils.hash covers the whole manifest, so a change of refreshPolicy, a label or
  a comment restarted the one-replica database and broker.
  * Secret: its name, type, data and stringData.
  * ExternalSecret: the Secret it writes (target.name), where the values come from (secretStoreRef, data, dataFrom) and how
    they are put together (target.template's type, engineVersion, data and templateFrom).
  * Any other kind: the whole object, so nothing a file starts to render can be missed.
usage: |
  secret-rabbit-admin-hash: {{ tuple "secret-rabbit-admin.yaml" . | include "helm-toolkit.utils.secret_data_hash" }}
*/}}
{{- define "helm-toolkit.utils.secret_data_hash" -}}
{{- $name := index . 0 -}}
{{- $context := index . 1 -}}
{{- $last := base $context.Template.Name -}}
{{- $file := $context.Template.Name | replace $last $name -}}
{{- $parts := list -}}
{{- range $doc := regexSplit "(?m)^---[ \t]*$" (include $file $context) -1 -}}
{{- $d := fromYaml $doc -}}
{{- if hasKey $d "Error" -}}{{- fail (printf "secret_data_hash: %s: %s" $name $d.Error) -}}{{- end -}}
{{- if $d -}}
{{- if eq ($d.kind | default "") "Secret" -}}
{{- $parts = append $parts (dict "kind" "Secret" "name" $d.metadata.name "type" $d.type "data" $d.data "stringData" $d.stringData) -}}
{{- else if eq ($d.kind | default "") "ExternalSecret" -}}
{{- $target := $d.spec.target | default dict -}}
{{- $t := $target.template | default dict -}}
{{- $parts = append $parts (dict "kind" "ExternalSecret" "name" ($target.name | default $d.metadata.name) "store" $d.spec.secretStoreRef "data" $d.spec.data "dataFrom" $d.spec.dataFrom "template" (dict "type" $t.type "engineVersion" $t.engineVersion "data" $t.data "templateFrom" $t.templateFrom)) -}}
{{- else -}}
{{- $parts = append $parts $d -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $parts | toJson | sha256sum | quote -}}
{{- end -}}
