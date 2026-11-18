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
  Adds custom annotations to the pod spec of a component.
examples:
  - values: |
      annotations:
        pod:
          default:
            custom.tld/key: "value"
            custom.tld/key2: "value2"
          nova_compute:
            another.tld/foo: "bar"
    usage: |
      {{ tuple "nova_compute" . | include "helm-toolkit.snippets.custom_pod_annotations" }}
    return: |
      another.tld/foo: bar
  - values: |
      annotations:
        pod:
          default:
            custom.tld/key: "value"
            custom.tld/key2: "value2"
          nova_compute:
            another.tld/foo: "bar"
    usage: |
      {{ tuple "nova_api" . | include "helm-toolkit.snippets.custom_pod_annotations" }}
    return: |
      custom.tld/key: "value"
      custom.tld/key2: "value2"
  - values: |
      annotations:
        pod:
          default:
            custom.tld/key: "value"
            custom.tld/key2: "value2"
          nova_compute:
            another.tld/foo: "bar"
          nova_api:
    usage: |
      {{ tuple "nova_api" . | include "helm-toolkit.snippets.custom_pod_annotations" }}
    return: |
      custom.tld/key: "value"
      custom.tld/key2: "value2"
*/}}

{{- define "helm-toolkit.snippets.custom_pod_annotations" -}}
{{- $component := index . 0 -}}
{{- $envAll := index . 1 -}}
{{- $annotations := dict -}}
{{- if (hasKey $envAll.Values "annotations") -}}
{{- if (hasKey $envAll.Values.annotations "pod") -}}
{{- $annotationsMap := $envAll.Values.annotations.pod -}}
{{- $defaultAnnotations := dict -}}
{{- if (hasKey $annotationsMap "default" ) -}}
{{- $defaultAnnotations = $annotationsMap.default -}}
{{- end -}}
{{- $annotations = index $annotationsMap $component | default $defaultAnnotations | default dict -}}
{{- end -}}
{{- end -}}
{{- /* Krateo (#309 C2): the RabbitMQ user generation this component's pods connect as. A rotation switches
       endpoints.oslo_messaging.auth.<component> to a new root and username, which reaches the pods only through Secrets
       ESO projects: no pod template changes with it (no hash covers a projected Secret), so without this a pod would
       keep the retired credential until it restarted for some other reason, and be locked out when its user is
       deleted. Every pod template of the chart includes this snippet, so every pod rolls on a switch
       (hack/validate-messaging-rotation.py renders that). Absent until a first rotation: an upgrade rolls nothing. */ -}}
{{- $sfx := (((($envAll.Values.endpoints | default dict).oslo_messaging | default dict).operator | default dict).activeSuffix | default "") | toString -}}
{{- if $sfx -}}
{{- $annotations = mergeOverwrite (deepCopy $annotations) (dict "krateo.io/messaging-generation" $sfx) -}}
{{- end -}}
{{- if (not (empty $annotations)) -}}
{{- toYaml $annotations -}}
{{- end -}}
{{- end -}}
