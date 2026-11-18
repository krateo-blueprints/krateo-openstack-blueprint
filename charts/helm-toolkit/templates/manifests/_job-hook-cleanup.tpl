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
Krateo blueprint addition (#206). Every Job is a post-install,post-upgrade hook (#179, 0.3.41), and Helm never deletes
hook resources at uninstall, so a removed component left its completed Jobs and their pods behind (#204: six cinder
Jobs, 156 pods across a full teardown). They cannot be hook-succeeded: kubernetes-entrypoint waits on a dependency Job
by name for as long as the component is installed. So each chart renders this pre-delete hook, which deletes the
release's install/upgrade hook Jobs when the release is uninstalled, and itself on success.

  - The Job is the last pre-delete hook (weight 100), after the teardown Jobs (db-drop, clean).
  - Its ServiceAccount, Role and RoleBinding are plain release objects, so they exist while pre-delete hooks run and
    are deleted with the release. The Role grants list and delete on Jobs in the release's namespace, nothing else.
  - It selects release_group=<this release>, which helm-toolkit puts on every Job a chart renders, minus itself, and
    deletes only the Jobs whose helm.sh/hook names post-install or post-upgrade (helm-toolkit.scripts.hook_cleanup).
  - It always exits 0, so a failed cleanup cannot block the uninstall. Its image is one the chart's own Jobs already run
    python in, as the image's default user (the openstack client of ks-user and rabbit-init, or db-init.py), so no new
    image is pulled and no new user is assumed. A pod that cannot start at all still fails the hook at its 300 s
    deadline, as any pre-delete hook (db-drop, clean) would.

hack/validate-hook-cleanup.py renders every chart with hook Jobs and checks all of the above, including a run of the
script against a stand-in API.

Usage, in a chart's templates/job-hook-cleanup.yaml:
  {- $job := dict "envAll" . "serviceName" "cinder" "image" .Values.images.tags.ks_user -}
  {- if .Values.pod.tolerations.cinder.enabled -}{- $_ := set $job "tolerationsEnabled" true -}{- end -}
  { $job | include "helm-toolkit.manifests.job_hook_cleanup" }
*/}}

{{- define "helm-toolkit.manifests.job_hook_cleanup" -}}
{{- $envAll := index . "envAll" -}}
{{- $serviceName := index . "serviceName" -}}
{{- $image := index . "image" | required "helm-toolkit.manifests.job_hook_cleanup needs an image with python" -}}
{{- $nodeSelector := index . "nodeSelector" -}}
{{- if not $nodeSelector -}}
{{- $nodeSelector = dict $envAll.Values.labels.job.node_selector_key $envAll.Values.labels.job.node_selector_value -}}
{{- end -}}
{{- $tolerationsEnabled := index . "tolerationsEnabled" | default false -}}
{{- $serviceNamePretty := $serviceName | replace "_" "-" -}}
{{- $name := printf "%s-hook-cleanup" $serviceNamePretty -}}
{{- $releaseGroup := $envAll.Values.release_group | default $envAll.Release.Name -}}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ $name }}
  namespace: {{ $envAll.Release.Namespace }}
{{- if dig "oci_image_registry" "auth" "enabled" false ($envAll.Values.endpoints | default dict) }}
imagePullSecrets:
  - name: {{ index $envAll.Values.secrets.oci_image_registry $envAll.Chart.Name }}
{{- end }}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {{ $name }}
  namespace: {{ $envAll.Release.Namespace }}
rules:
  - apiGroups:
      - batch
    resources:
      - jobs
    verbs:
      - list
      - delete
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {{ $name }}
  namespace: {{ $envAll.Release.Namespace }}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: {{ $name }}
subjects:
  - kind: ServiceAccount
    name: {{ $name }}
    namespace: {{ $envAll.Release.Namespace }}
---
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ $name | quote }}
  labels:
{{ tuple $envAll $serviceName "hook-cleanup" | include "helm-toolkit.snippets.kubernetes_metadata_labels" | indent 4 }}
  annotations:
    "helm.sh/hook": pre-delete
    "helm.sh/hook-weight": "100"
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  backoffLimit: 2
{{ tuple $envAll 300 | include "helm-toolkit.snippets.job_hook_deadline" | indent 2 }}
  template:
    metadata:
      labels:
{{ tuple $envAll $serviceName "hook-cleanup" | include "helm-toolkit.snippets.kubernetes_metadata_labels" | indent 8 }}
    spec:
      serviceAccountName: {{ $name }}
      restartPolicy: OnFailure
{{- $pullSecrets := tuple $envAll "hook_cleanup" | include "helm-toolkit.snippets.kubernetes_image_pull_secrets" }}
{{- if $pullSecrets }}
{{ $pullSecrets | indent 6 }}
{{- end }}
      nodeSelector:
{{ toYaml $nodeSelector | indent 8 }}
{{- if $tolerationsEnabled }}
{{ tuple $envAll $serviceName | include "helm-toolkit.snippets.kubernetes_tolerations" | indent 6 }}
{{- end }}
      containers:
        - name: hook-cleanup
          image: {{ $image }}
          imagePullPolicy: {{ $envAll.Values.images.pull_policy }}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop:
                - ALL
          env:
            - name: NAMESPACE
              valueFrom:
                fieldRef:
                  fieldPath: metadata.namespace
            - name: SELECTOR
              value: {{ printf "release_group=%s,component!=hook-cleanup" $releaseGroup | quote }}
          command:
            - python
            - -c
            - |
{{ include "helm-toolkit.scripts.hook_cleanup" $envAll | trim | indent 14 }}
{{- end -}}
