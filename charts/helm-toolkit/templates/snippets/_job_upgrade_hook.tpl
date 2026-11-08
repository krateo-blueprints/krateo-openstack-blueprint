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
  Makes a Job a Helm post-install,post-upgrade hook, deleted and re-created on every upgrade.

  A Job's pod template is immutable, and Krateo's composition-dynamic-controller patches a plain
  Job in place, so any release that changes one (an image bump is enough) failed that
  component's upgrade with "cannot patch ... spec.template: Invalid value" (#179). As a hook
  the Job is never patched: before-hook-creation deletes the old one, then the new one runs.
  Measured on GKE with core-provider 2.13.72: hooks run on install and on upgrade, the
  controller does not wait for workloads to be Ready first, and a Job that was plain on a
  failed release is replaced by its hook (#179).

  Hooks run one at a time, in weight order, each waited for until it completes. So a Job must
  sort after every Job it waits on through kubernetes-entrypoint, directly or through the pods
  behind a service it waits on. The weight comes from the Job's role, in bands of dependency
  depth measured across every chart; hack/validate-job-hooks.py proves the order per chart.
  An unknown role fails the render rather than guessing a weight.

  Never hook-succeeded: kubernetes-entrypoint finds a completed Job by name, so a Job deleted
  on success would leave every pod that waits on it waiting forever.

  Set helm3_hook: false only to render plain Jobs; that brings the #179 failure back, and the
  gate refuses it.
values: |
  helm3_hook: true
usage: |
  {{ tuple "db-sync" . | include "helm-toolkit.snippets.job_upgrade_hook" }}
return: |
  helm.sh/hook: post-install,post-upgrade
  helm.sh/hook-delete-policy: before-hook-creation
  helm.sh/hook-weight: "-9"
*/}}

{{- define "helm-toolkit.snippets.job_upgrade_hook" -}}
{{- $role := index . 0 -}}
{{- $envAll := index . 1 -}}
{{- $weights := dict
      "api-key-init" "-10"
      "clean" "-10"
      "cluster-wait" "-10"
      "create-internal-tenant" "-10"
      "credential-setup" "-10"
      "db-init" "-10"
      "db-init-indexer" "-10"
      "domain-ks-user" "-10"
      "exporter-create-user" "-10"
      "fernet-setup" "-10"
      "image-repo-sync" "-10"
      "ks-service" "-10"
      "ks-user" "-10"
      "manage-cleaning-network" "-10"
      "rabbit-init" "-10"
      "ring-builder" "-10"
      "s3-user" "-10"
      "db-sync" "-9"
      "ks-endpoints" "-9"
      "s3-bucket" "-9"
      "trusts" "-9"
      "backup-storage-init" "-8"
      "cell-setup" "-8"
      "domain-manage" "-8"
      "metadefs-load" "-8"
      "storage-init" "-8"
      "bootstrap" "-7"
-}}
{{- if not (hasKey $weights $role) -}}
{{- fail (printf "helm-toolkit.snippets.job_upgrade_hook: no hook weight for Job role %q; add it to the table in _job_upgrade_hook.tpl in the band of its dependency depth, and run hack/validate-job-hooks.py" $role) -}}
{{- end -}}
{{- if ne (toString $envAll.Values.helm3_hook) "false" -}}
helm.sh/hook: post-install,post-upgrade
helm.sh/hook-delete-policy: before-hook-creation
helm.sh/hook-weight: {{ index $weights $role | quote }}
{{- end -}}
{{- end -}}
