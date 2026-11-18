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
Filter dependency jobs when the corresponding manifests.job_* flag is disabled.
The manifest key is derived from the dependency job name by dropping the chart
prefix up to the first dash and converting the remainder from kebab-case to
snake_case. If no matching manifest key exists, the job is kept.
*/}}
{{- define "helm-toolkit.utils.dependency_jobs_filter" -}}
{{- $envAll := index . "envAll" -}}
{{- $deps := index . "deps" -}}
{{- /* Krateo (#309 H2b): with the RabbitMQ cluster-operator (endpoints.oslo_messaging.operator.cluster), a
       <serviceUser>-rabbit-init Job is not rendered: helm-toolkit.manifests.rabbitmq_topology renders that user's
       messaging-topology-operator resources instead. A pod that waited on the Job waits on those resources being
       Ready instead (kubernetes-entrypoint custom_resources by condition; kubernetes_pod_rbac_serviceaccount grants the
       get, #370). Applied first, before the manifests.job_* filter below, and idempotent: this runs once for the RBAC
       and again for the init container on the same dependency map. */ -}}
{{- /* Only where this chart runs its rabbit-init (manifests.job_rabbit_init true or absent): with it off, the filter
       below drops the Job wait, and waiting on resources nothing renders would hold the pod in Init forever. */ -}}
{{- $rabbitInit := or (not (hasKey ($envAll.Values.manifests | default dict) "job_rabbit_init")) (index $envAll.Values.manifests "job_rabbit_init") -}}
{{- if and $deps (hasKey $deps "jobs") $deps.jobs (kindIs "string" (index $deps.jobs 0)) $rabbitInit ((($envAll.Values.endpoints.oslo_messaging | default dict).operator | default dict).cluster) -}}
{{- $kept := list -}}
{{- $crs := $deps.custom_resources | default list -}}
{{- range $job := $deps.jobs -}}
{{- if not (hasSuffix "-rabbit-init" $job) -}}{{- $kept = append $kept $job -}}{{- end -}}
{{- end -}}
{{- range $cr := include "helm-toolkit.utils.rabbitmq_topology_waits" (dict "jobs" $deps.jobs "suffix" (($envAll.Values.endpoints.oslo_messaging.operator | default dict).activeSuffix | default "")) | fromJsonArray -}}
{{- $seen := false -}}
{{- range $have := $crs -}}{{- if and (eq $have.kind $cr.kind) (eq $have.name $cr.name) -}}{{- $seen = true -}}{{- end -}}{{- end -}}
{{- if not $seen -}}{{- $crs = append $crs $cr -}}{{- end -}}
{{- end -}}
{{- $_ := set $deps "jobs" $kept -}}
{{- $_ := set $deps "custom_resources" $crs -}}
{{- end -}}
{{- if and $deps (hasKey $deps "jobs") $deps.jobs -}}
{{- if kindIs "string" (index $deps.jobs 0) -}}
{{- $jobs := list -}}
{{- range $job := $deps.jobs -}}
{{- $jobParts := splitList "-" $job -}}
{{- if gt (len $jobParts) 1 -}}
{{- $manifestKey := printf "job_%s" (join "_" (rest $jobParts)) -}}
{{- if or (not (hasKey $envAll.Values.manifests $manifestKey)) (index $envAll.Values.manifests $manifestKey) -}}
{{- $jobs = append $jobs $job -}}
{{- end -}}
{{- else -}}
{{- $jobs = append $jobs $job -}}
{{- end -}}
{{- end -}}
{{- $_ := set $deps "jobs" $jobs -}}
{{- end -}}
{{- end -}}
{{ $deps | toYaml }}
{{- end -}}

{{/* Krateo (#309 H2b): the messaging-topology-operator resources that stand for each <serviceUser>-rabbit-init in a
     list of Job names, as a JSON list of kubernetes-entrypoint custom_resources entries (Ready condition, resource
     plural for the Role). The names are helm-toolkit.manifests.rabbitmq_topology's. Used by dependency_jobs_filter
     for the wait, and by kubernetes_pod_rbac_serviceaccount for the grant of rabbit-init waits that only the init
     container adds (dependencies.dynamic.job_rabbit_init). */}}
{{- define "helm-toolkit.utils.rabbitmq_topology_waits" -}}
{{- /* (jobs) or (dict "jobs" jobs "suffix" activeSuffix): the topology-resource waits replacing each *-rabbit-init Job
       wait. They name the ACTIVE generation's User and Permission (<su>-rabbitmq-user<suffix>, #309 C2: during and after
       a rotation the generation-0 user is deleted, and a pod waiting on it would stay in Init forever). */ -}}
{{- $jobs := . -}}{{- $sfx := "" -}}
{{- if kindIs "map" . -}}{{- $jobs = .jobs -}}{{- $sfx = (.suffix | default "") | toString -}}{{- end -}}
{{- $out := list -}}
{{- range $job := ($jobs | default list) -}}
{{- if and (kindIs "string" $job) (hasSuffix "-rabbit-init" $job) -}}
{{- $su := trimSuffix "-rabbit-init" $job -}}
{{- range $r := list (list "User" "users" (printf "user%s" $sfx)) (list "Vhost" "vhosts" "vhost") (list "Permission" "permissions" (printf "permission%s" $sfx)) (list "Permission" "permissions" "admin-permission") -}}
{{- $out = append $out (dict "apiVersion" "rabbitmq.com/v1beta1" "kind" (index $r 0) "resource" (index $r 1) "name" (printf "%s-rabbitmq-%s" $su (index $r 2)) "conditions" (list (dict "type" "Ready"))) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}
