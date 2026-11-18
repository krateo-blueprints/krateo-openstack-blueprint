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

{{- define "helm-toolkit.snippets.kubernetes_pod_rbac_serviceaccount" -}}
{{- $envAll := index . 0 -}}
{{- $component := index . 1 -}}
{{- $saName := index . 2 -}}
{{- $saNamespace := $envAll.Release.Namespace }}
{{- $randomKey := randAlphaNum 32 }}
{{- $allNamespace := dict $randomKey "" }}

{{- $_ := set $envAll.Values "__kubernetes_entrypoint_init_container" dict -}}
{{- $_ := set $envAll.Values.__kubernetes_entrypoint_init_container "deps" dict -}}
{{- if and ($envAll.Values.images.local_registry.active) (ne $component "image_repo_sync") -}}
{{- if eq $component "pod_dependency" -}}
{{- $_ := include "helm-toolkit.utils.merge" ( tuple $envAll.Values.__kubernetes_entrypoint_init_container.deps ( index $envAll.Values.pod_dependency ) $envAll.Values.dependencies.dynamic.common.local_image_registry ) -}}
{{- else -}}
{{- $_ := include "helm-toolkit.utils.merge" ( tuple $envAll.Values.__kubernetes_entrypoint_init_container.deps ( index $envAll.Values.dependencies.static $component ) $envAll.Values.dependencies.dynamic.common.local_image_registry ) -}}
{{- end -}}
{{- else -}}
{{- if eq $component "pod_dependency" -}}
{{- $_ := set $envAll.Values.__kubernetes_entrypoint_init_container "deps" ( index $envAll.Values.pod_dependency ) -}}
{{- else -}}
{{- $_ := set $envAll.Values.__kubernetes_entrypoint_init_container "deps" ( index $envAll.Values.dependencies.static $component ) -}}
{{- end -}}
{{- end -}}
{{- $_ := include "helm-toolkit.utils.dependency_jobs_filter" (dict "envAll" $envAll "deps" $envAll.Values.__kubernetes_entrypoint_init_container.deps) | toString | fromYaml -}}
{{- $deps := $envAll.Values.__kubernetes_entrypoint_init_container.deps }}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ $saName }}
  namespace: {{ $saNamespace }}
{{- if dig "oci_image_registry" "auth" "enabled" false ($envAll.Values.endpoints | default dict) }}
imagePullSecrets:
  - name: {{ index $envAll.Values.secrets.oci_image_registry $envAll.Chart.Name }}
{{- end -}}
{{- range $k, $v := $deps -}}
{{- if eq $k "services" }}
{{- range $serv := $v }}
{{- $endpointMap := index $envAll.Values.endpoints $serv.service }}
{{- $endpointNS := $endpointMap.namespace | default $saNamespace }}
{{- if not (contains "services" ((index $allNamespace $endpointNS) | default "")) }}
{{- $_ := set $allNamespace $endpointNS (printf "%s%s" "services," ((index $allNamespace $endpointNS) | default "")) }}
{{- end -}}
{{- end -}}
{{- else if and (eq $k "jobs") $v }}
{{- $_ := set $allNamespace $saNamespace  (printf "%s%s" "jobs," ((index $allNamespace $saNamespace) | default "")) }}
{{- else if and (eq $k "daemonset") $v }}
{{- $_ := set $allNamespace $saNamespace  (printf "%s%s" "daemonsets," ((index $allNamespace $saNamespace) | default "")) }}
{{- else if and (eq $k "pod") $v }}
{{- $_ := set $allNamespace $saNamespace  (printf "%s%s" "pods," ((index $allNamespace $saNamespace) | default "")) }}
{{- else if and (eq $k "secret") $v }}
{{- $_ := set $allNamespace $saNamespace  (printf "%s%s" "secrets," ((index $allNamespace $saNamespace) | default "")) }}
{{- end -}}
{{- end -}}
{{- $_ := unset $allNamespace $randomKey }}
{{- range $ns, $vv := $allNamespace }}
{{- $resourceList := (splitList "," (trimSuffix "," $vv)) }}
{{- tuple $envAll $resourceList $ns $saName $saNamespace | include "helm-toolkit.snippets.kubernetes_pod_rbac_roles" }}
{{- end -}}
{{- /* Krateo (#309): kubernetes-entrypoint waits on custom resources (dependencies custom_resources, matched by fields
       or by status.conditions) by reading each one with a named GET, after discovering its group/version. The Roles
       above cover only core, batch, apps and discovery resources, so without this every such wait is Forbidden and
       the pod stays in Init. Each entry names its `resource` plural (kubernetes-entrypoint ignores the extra key);
       the Role grants get on exactly that object, in the release namespace where the init container looks it up.
       hack/validate-custom-resource-wait-rbac.py renders this. */}}
{{- $crWaits := $deps.custom_resources | default list }}
{{- /* With the RabbitMQ cluster-operator, the rabbit-init waits the init container adds from
       dependencies.dynamic.job_rabbit_init.<component> become topology-resource waits there
       (helm-toolkit.utils.dependency_jobs_filter); grant them here too, or those waits are Forbidden. */}}
{{- if and ((($envAll.Values.endpoints.oslo_messaging | default dict).operator | default dict).cluster) $envAll.Values.manifests.job_rabbit_init (hasKey $envAll.Values.dependencies "dynamic") }}
{{- with (index ($envAll.Values.dependencies.dynamic.job_rabbit_init | default dict) $component) }}
{{- $crWaits = concat $crWaits (include "helm-toolkit.utils.rabbitmq_topology_waits" (dict "jobs" .jobs "suffix" (($envAll.Values.endpoints.oslo_messaging.operator | default dict).activeSuffix | default "")) | fromJsonArray) }}
{{- end }}
{{- end }}
{{- $crRules := list }}
{{- range $cr := $crWaits }}
{{- $parts := splitList "/" ($cr.apiVersion | default "" | toString) }}
{{- if or (ne (len $parts) 2) (not (index $parts 0)) }}
{{- fail (printf "dependencies custom_resources entry %q (kind %v): apiVersion %q must be <group>/<version>; kubernetes-entrypoint reads custom resources only (#309)" ($cr.name | default "") $cr.kind ($cr.apiVersion | default "")) }}
{{- end }}
{{- if not $cr.resource }}
{{- fail (printf "dependencies custom_resources entry %q (%s %v) needs `resource`, its plural (e.g. users), so the Role can grant kubernetes-entrypoint get on it; without it the wait is Forbidden and the pod never leaves Init (#309)" ($cr.name | default "") $cr.apiVersion $cr.kind) }}
{{- end }}
{{- $crRules = append $crRules (dict "group" (index $parts 0) "resource" ($cr.resource | toString) "name" ($cr.name | toString)) }}
{{- end }}
{{- if $crRules }}
{{- $crRole := printf "%s-%s-%s-custom-resources" $envAll.Release.Name $saNamespace ($saName | replace "_" "-") }}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {{ $crRole }}
  namespace: {{ $envAll.Release.Namespace }}
rules:
{{- range $r := $crRules }}
  - apiGroups:
      - {{ $r.group | quote }}
    resources:
      - {{ $r.resource | quote }}
    resourceNames:
      - {{ $r.name | quote }}
    verbs:
      - get
{{- end }}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {{ $crRole }}
  namespace: {{ $envAll.Release.Namespace }}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: {{ $crRole }}
subjects:
  - kind: ServiceAccount
    name: {{ $saName }}
    namespace: {{ $saNamespace }}
{{- end }}
{{- end -}}
