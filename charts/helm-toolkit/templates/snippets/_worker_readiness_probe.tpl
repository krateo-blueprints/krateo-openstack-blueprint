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
  "config" is the program's configuration list, the SAME `<chart>.config_args.<program>` definition its
  start script's exec line renders through helm-toolkit.snippets.oslo_config_args, so the probe reads
  the configuration the program was started with (required for --rpc-topic and --broker; --listen
  needs none). The container must mount the chart's `worker-health-probe.py` ConfigMap key at
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
  {{ dict "envAll" $envAll "component" "applier" "container" "default" "args" (list "--process" "watcher-applier" "--exchange" "watcher" "--rpc-topic" "watcher.applier.control") "config" (include "watcher.config_args.applier" $envAll | fromYamlArray) | include "helm-toolkit.snippets.worker_readiness_probe" | indent 10 }}
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
        - --config-file
        - /etc/watcher/watcher.conf
        - --config-dir-if-present
        - /etc/watcher/watcher.conf.d
    initialDelaySeconds: 20
    periodSeconds: 60
    timeoutSeconds: 50
*/}}

{{- define "helm-toolkit.snippets.worker_readiness_probe" -}}
{{- $command := concat (list "python" "/tmp/worker-health-probe.py") (index . "args") -}}
{{- with index . "config" -}}
{{- $command = concat $command (include "helm-toolkit.snippets.oslo_config_args" (dict "args" . "format" "probe") | fromJsonArray) -}}
{{- end -}}
{{- $probe := dict "exec" (dict "command" $command) -}}
{{- dict "envAll" (index . "envAll") "component" (index . "component") "container" (index . "container") "type" "readiness" "probeTemplate" $probe | include "helm-toolkit.snippets.kubernetes_probe" -}}
{{- end -}}
