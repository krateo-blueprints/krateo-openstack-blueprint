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
  Bounds a hook Job's run time with spec.activeDeadlineSeconds, so a hook that can never
  finish fails instead of holding its release's Helm operation open forever.

  Helm waits for every hook Job to complete, and the composition-dynamic-controller sets no
  limit of its own on a Helm operation (#198). So an unbounded hook whose pod can never start
  keeps the release in pending-install for as long as it exists, and the Composition shows no
  condition at all: measured on a cold 0.3.42 install, cinder-storage-init waited on Ceph
  objects nothing creates for over 45 minutes and cinder reported nothing (#204, #196).

  The default, 1800s, rests on two measurements on GKE with core-provider 2.13.72: on a cold
  0.3.42 install the slowest of 161 Jobs took 101s (p90 61s), and the slowest Job ever
  measured, on the #179 upgrade, took 672s while waiting for slower components. 1800s is
  about 2.7 times the worst case seen, and still makes a hang visible within half an hour.

  Precedence: a positive per-Job value the template passes in (the existing
  jobs.<name>.activeDeadlineSeconds values), then a positive chart-wide
  helm3_hook_deadline_seconds, then 1800. Zero or unset falls through, so no hook Job is ever
  rendered unbounded; hack/validate-job-hooks.py refuses one that is.
values: |
  helm3_hook_deadline_seconds: 1800
usage: |
  spec:
  {{ tuple $envAll $activeDeadlineSeconds | include "helm-toolkit.snippets.job_hook_deadline" | indent 2 }}
return: |
  activeDeadlineSeconds: 1800
*/}}

{{- define "helm-toolkit.snippets.job_hook_deadline" -}}
{{- $envAll := index . 0 -}}
{{- $explicit := index . 1 -}}
{{- $seconds := 1800 -}}
{{- $chartWide := $envAll.Values.helm3_hook_deadline_seconds -}}
{{- if and $chartWide (gt (int $chartWide) 0) -}}
{{- $seconds = int $chartWide -}}
{{- end -}}
{{- if and $explicit (gt (int $explicit) 0) -}}
{{- $seconds = int $explicit -}}
{{- end -}}
activeDeadlineSeconds: {{ $seconds }}
{{- end -}}
