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

# This function creates a manifest for keystone user management.
# It can be used in charts dict created similar to the following:
# {- $ksUserJob := dict "envAll" . "serviceName" "senlin" }
# { $ksUserJob | include "helm-toolkit.manifests.job_ks_user" }

{{/*
# This function creates a manifest for keystone user management.
# It can be used in charts as follows:
# {{- $ksUserJob := dict "envAll" . "serviceName" "heat" "serviceUsers" ( tuple "heat" "heat_trustee" ) -}}
# {{ $ksUserJob | include "helm-toolkit.manifests.job_ks_user" }}
*/}}

{{- define "helm-toolkit.manifests.job_ks_user" -}}
{{- $envAll := index . "envAll" -}}
{{- $serviceName := index . "serviceName" -}}
{{- $serviceNamePretty := $serviceName | replace "_" "-" -}}
{{- $jobAnnotations := index . "jobAnnotations" -}}
{{- $jobLabels := index . "jobLabels" -}}
{{- $nodeSelector := index . "nodeSelector" | default ( dict $envAll.Values.labels.job.node_selector_key $envAll.Values.labels.job.node_selector_value ) -}}
{{- $tolerationsEnabled := index . "tolerationsEnabled" | default false -}}
{{- $configMapBin := index . "configMapBin" | default (printf "%s-%s" $serviceName "bin" ) -}}
{{- $singleServiceUser := index . "serviceUser" | default $serviceName -}}
{{- $serviceUsers := index . "serviceUsers" | default (tuple $singleServiceUser) -}}
{{- $secretBin := index . "secretBin" -}}
{{- $tlsSecret := index . "tlsSecret" | default "" -}}
{{- $backoffLimit := index . "backoffLimit" | default "1000" -}}
{{- $activeDeadlineSeconds := index . "activeDeadlineSeconds" -}}
{{- $restartPolicy_ := "OnFailure" -}}
{{- if hasKey $envAll.Values "jobs" -}}
{{- if hasKey $envAll.Values.jobs "ks_user" -}}
{{- $restartPolicy_ = $envAll.Values.jobs.ks_user.restartPolicy | default $restartPolicy_ }}
{{- end }}
{{- end }}
{{- $restartPolicy := index . "restartPolicy" | default $restartPolicy_ -}}
{{- $ksUserScript := index . "ksUserScript" | default (include "helm-toolkit.scripts.keystone_user" (dict "krateoRotate" true)) -}}

{{- $serviceAccountName := printf "%s-ks-user" $serviceNamePretty }}
{{ tuple $envAll "ks_user" $serviceAccountName | include "helm-toolkit.snippets.kubernetes_pod_rbac_serviceaccount" }}
{{- /* Krateo (#228): users whose password comes from the umbrella's root (passwordFrom) are rotated by a
       PRE-upgrade Job, <svc>-ks-user, so Keystone holds the new password before any reader restarts onto it.
       A pre-upgrade hook runs before this release's resources are applied, so that Job reads nothing the
       release renders: its script is inline (not the previous revision's <svc>-bin), the user's fields come
       from values and the password from the root. Users still set from values keep upstream's post-upgrade
       Job reading their openrc Secret (named <svc>-ks-user-values when both kinds exist). */}}
{{- $refUsers := list -}}
{{- $valUsers := list -}}
{{- range $u := $serviceUsers }}
{{- $pf := (index $envAll.Values.endpoints.identity.auth $u).passwordFrom | default dict }}
{{- if $pf.secret }}{{- $refUsers = append $refUsers $u -}}{{- else }}{{- $valUsers = append $valUsers $u -}}{{- end }}
{{- end }}
{{- $jobs := list -}}
{{- if $refUsers }}
{{- $jobs = append $jobs (dict "name" (printf "%s-ks-user" $serviceNamePretty) "users" $refUsers "rotate" true) -}}
{{- end }}
{{- if $valUsers }}
{{- $jobs = append $jobs (dict "name" (printf "%s-ks-user%s" $serviceNamePretty (ternary "-values" "" (gt (len $refUsers) 0))) "users" $valUsers "rotate" false) -}}
{{- end }}
{{- range $job := $jobs }}
---
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ $job.name | quote }}
  labels:
{{ tuple $envAll $serviceName "ks-user" | include "helm-toolkit.snippets.kubernetes_metadata_labels" | indent 4 }}
{{- if $jobLabels }}
{{ toYaml $jobLabels | indent 4 }}
{{- end }}
  annotations:
{{ tuple "ks-user" $envAll (ternary "pre-upgrade" "post-upgrade" $job.rotate) | include "helm-toolkit.snippets.job_upgrade_hook" | indent 4 }}
{{ tuple $serviceAccountName $envAll | include "helm-toolkit.snippets.custom_job_annotations" | indent 4 -}}
{{- if $jobAnnotations }}
{{ toYaml $jobAnnotations | indent 4 }}
{{- end }}
spec:
  backoffLimit: {{ $backoffLimit }}
{{ tuple $envAll $activeDeadlineSeconds | include "helm-toolkit.snippets.job_hook_deadline" | indent 2 }}
  template:
    metadata:
      labels:
{{ tuple $envAll $serviceName "ks-user" | include "helm-toolkit.snippets.kubernetes_metadata_labels" | indent 8 }}
{{- if $jobLabels }}
{{ toYaml $jobLabels | indent 8 }}
{{- end }}
      annotations:
{{ tuple $envAll | include "helm-toolkit.snippets.release_uuid" | indent 8 }}
    spec:
      serviceAccountName: {{ $serviceAccountName | quote }}
{{ dict "envAll" $envAll "application" "ks_user" | include "helm-toolkit.snippets.kubernetes_pod_security_context" | indent 6 }}
      restartPolicy: {{ $restartPolicy }}
      {{ tuple $envAll "ks_user" | include "helm-toolkit.snippets.kubernetes_image_pull_secrets" | indent 6 }}
      nodeSelector:
{{ toYaml $nodeSelector | indent 8 }}
{{- if $tolerationsEnabled }}
{{ tuple $envAll $serviceName | include "helm-toolkit.snippets.kubernetes_tolerations" | indent 6 }}
{{- end}}
      initContainers:
{{ tuple $envAll "ks_user" list | include "helm-toolkit.snippets.kubernetes_entrypoint_init_container" | indent 8 }}
      containers:
{{- range $serviceUser := $job.users }}
        - name: {{ printf "%s-ks-user" $serviceUser | replace "_" "-" | quote }}
          image: {{ $envAll.Values.images.tags.ks_user }}
          imagePullPolicy: {{ $envAll.Values.images.pull_policy }}
{{ tuple $envAll $envAll.Values.pod.resources.jobs.ks_user | include "helm-toolkit.snippets.kubernetes_resources" | indent 10 }}
{{ dict "envAll" $envAll "application" "ks_user" "container" "ks_user" | include "helm-toolkit.snippets.kubernetes_container_security_context" | indent 10 }}
          command:
            - /bin/bash
            - -c
{{- if $job.rotate }}
            - {{ $ksUserScript | toJson }}
{{- else }}
            - /tmp/ks-user.sh
{{- end }}
          volumeMounts:
            - name: pod-tmp
              mountPath: /tmp
{{- if not $job.rotate }}
            - name: ks-user-sh
              mountPath: /tmp/ks-user.sh
              subPath: ks-user.sh
              readOnly: true
{{- end }}
{{ dict "enabled" true "name" $tlsSecret "ca" true | include "helm-toolkit.snippets.tls_volume_mount" | indent 12 }}
          env:
{{- $identityOpenrc := dict }}
{{- if hasKey $envAll.Values "identity" }}
{{- if hasKey $envAll.Values.identity "openrc" }}
{{- $identityOpenrc = $envAll.Values.identity.openrc }}
{{- end }}
{{- end }}
{{- $env := dict "ksUserSecret" $envAll.Values.secrets.identity.admin -}}
{{- $_ := set $env "useCA" (ne $tlsSecret "") -}}
{{- $_ := set $env "excludeVars" ($identityOpenrc.exclude_vars | default list) -}}
{{- $_ := set $env "extraVars" ($identityOpenrc.extra_vars | default list) -}}
{{- include "helm-toolkit.snippets.keystone_openrc_env_vars" $env | indent 12 }}
            - name: SERVICE_OS_SERVICE_NAME
              value: {{ $serviceName | quote }}
{{- $user := index $envAll.Values.endpoints.identity.auth $serviceUser }}
{{- with $env := dict "ksUserSecret" (index $envAll.Values.secrets.identity $serviceUser ) "passwordFrom" (ternary ($user.passwordFrom | default dict) dict $job.rotate) "user" $user }}
{{- include "helm-toolkit.snippets.keystone_user_create_env_vars" $env | indent 12 }}
{{- end }}
            - name: SERVICE_OS_ROLES
            {{- $serviceOsRoles := index $envAll.Values.endpoints.identity.auth $serviceUser "role" }}
            {{- if kindIs "slice" $serviceOsRoles }}
              value: {{ include "helm-toolkit.utils.joinListWithComma" $serviceOsRoles | quote }}
            {{- else }}
              value: {{ $serviceOsRoles | quote }}
            {{- end }}
{{- end }}
      volumes:
        - name: pod-tmp
          emptyDir: {}
{{- if not $job.rotate }}
        - name: ks-user-sh
{{- if $secretBin }}
          secret:
            secretName: {{ $secretBin | quote }}
            defaultMode: 0555
{{- else }}
          configMap:
            name: {{ $configMapBin | quote }}
            defaultMode: 0555
{{- end }}
{{- end }}
{{- dict "enabled" true "name" $tlsSecret | include "helm-toolkit.snippets.tls_volume" | indent 8 }}
{{- end }}
{{- end -}}
