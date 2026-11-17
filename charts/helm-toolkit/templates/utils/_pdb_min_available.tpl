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
  A PodDisruptionBudget's minAvailable that never blocks a drain (#286). An integer min_available at or above the
  workload's replica count would refuse every eviction, so a node drain waits forever: with the default
  min_available 1, overriding replicas to 1 would do exactly that. It is lowered to replicas - 1 (0 for one replica,
  which then gets no protection, as one replica cannot be both kept and moved). A percentage passes through.
values: |
  pod:
    replicas:
      api: 1
    lifecycle:
      disruption_budget:
        api:
          min_available: 1
usage: |
  minAvailable: {{ tuple .Values.pod.lifecycle.disruption_budget.api.min_available .Values.pod.replicas.api | include "helm-toolkit.utils.pdb_min_available" }}
return: |
  minAvailable: 0
*/}}

{{- define "helm-toolkit.utils.pdb_min_available" -}}
{{- $min := index . 0 -}}
{{- $replicas := index . 1 | int -}}
{{- if and (kindIs "string" $min) (hasSuffix "%" $min) -}}
{{- $min | quote -}}
{{- else if ge ($min | int) $replicas -}}
{{- max 0 (sub $replicas 1) -}}
{{- else -}}
{{- $min | int -}}
{{- end -}}
{{- end -}}
