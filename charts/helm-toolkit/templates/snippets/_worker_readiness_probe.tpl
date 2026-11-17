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
  The readiness probe of an OpenStack worker with no HTTP endpoint (#231, #151): an exec of
  helm-toolkit.scripts.health_probe, rendered by helm-toolkit.snippets.kubernetes_probe so it is
  switched and tuned by pod.probes.<component>.<container>.readiness like every other probe.
  The container must mount the chart's `worker-health-probe.py` ConfigMap key at
  /tmp/worker-health-probe.py (hack/validate-readiness-probes.py fails a probe whose script is
  not mounted). Readiness only: see the script for why there is no liveness twin.
values: |
  pod:
    probes:
      applier:
        default:
          readiness:
            enabled: true
            params:
              initialDelaySeconds: 20
              periodSeconds: 60
              timeoutSeconds: 50
usage: |
  {{ dict "envAll" $envAll "component" "applier" "container" "default" "args" (list "--process" "watcher-applier" "--exchange" "watcher" "--rpc-topic" "watcher.applier.control") | include "helm-toolkit.snippets.worker_readiness_probe" | indent 10 }}
return: |
  readinessProbe:
    exec:
      command:
        - python
        - /tmp/worker-health-probe.py
        - --process
        - watcher-applier
        - --exchange
        - watcher
        - --rpc-topic
        - watcher.applier.control
    initialDelaySeconds: 20
    periodSeconds: 60
    timeoutSeconds: 50
*/}}

{{- define "helm-toolkit.snippets.worker_readiness_probe" -}}
{{- $probe := dict "exec" (dict "command" (concat (list "python" "/tmp/worker-health-probe.py") (index . "args"))) -}}
{{- dict "envAll" (index . "envAll") "component" (index . "component") "container" (index . "container") "type" "readiness" "probeTemplate" $probe | include "helm-toolkit.snippets.kubernetes_probe" -}}
{{- end -}}
