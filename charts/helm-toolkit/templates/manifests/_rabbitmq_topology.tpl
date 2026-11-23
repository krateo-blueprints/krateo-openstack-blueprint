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
Krateo (#309 H2b): a component's RabbitMQ user, vhost, permissions and policies as messaging-topology-operator custom
resources (rabbitmq.com/v1beta1), for a RabbitmqCluster run by the cluster-operator. It replaces the rabbit-init Job
when endpoints.oslo_messaging.operator.cluster names the cluster. One source of truth, as #228 wants:
  - the password is the user's generated root, endpoints.oslo_messaging.auth.<serviceName>.passwordFrom, projected
    with the username into <serviceUser>-rabbitmq-topology-user, which the User imports. Not <serviceUser>-rabbitmq-user:
    that is the name charts give their own connection Secret (secrets.oslo_messaging.<serviceName>, which still renders
    for its other readers), and helm refuses a release holding two ExternalSecrets of one name (H2 run 2, 2026-11-18),
    so a projection named like the chart's Secret is refused here. The topology operator watches that
    Secret and re-syncs the password when it changes (its User doc and controller), so a rotated root reaches RabbitMQ
    when the projection changes (refreshPolicy OnChange: on a rotation, never on a timer), with no Job;
  - the vhost is the one rabbit-init declared: endpoints.oslo_messaging.path without its leading slash;
  - the user gets configure, write and read ".*" on it, as rabbit-init granted, and so does the RabbitMQ
    administrator (endpoints.oslo_messaging.auth.admin.username): ceilometer's notification agent consumes other
    components' vhosts as the administrator (#281), which the cluster-operator does not grant by itself;
  - conf.rabbitmq.policies become Policy resources on that vhost, without the ha-* keys RabbitMQ 4.x refuses.
Names are <serviceUser>-rabbitmq-{user,vhost,permission,admin-permission,policy-<name>}, the names
helm-toolkit.utils.dependency_jobs_filter waits on in place of <serviceUser>-rabbit-init. The vhost is retained when
its resource is deleted (deletionPolicy retain: a pruned resource would otherwise delete the vhost and every queue in
it). hack/validate-messaging-topology.py renders all of it.
*/}}
{{- define "helm-toolkit.manifests.rabbitmq_topology" -}}
{{- $envAll := index . "envAll" -}}
{{- $serviceName := index . "serviceName" -}}
{{- $su := (index . "serviceUser" | default $serviceName) | replace "_" "-" -}}
{{- $om := $envAll.Values.endpoints.oslo_messaging -}}
{{- $cluster := $om.operator.cluster -}}
{{- $auth := index ($om.auth | default dict) $serviceName | default dict -}}
{{- $from := $auth.passwordFrom | default dict -}}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index $from $field) }}
{{- fail (printf "endpoints.oslo_messaging.auth.%s.passwordFrom.%s is required with the RabbitMQ cluster-operator: the user's password comes from its generated root (#228), never from a value" $serviceName $field) }}
{{- end }}
{{- end }}
{{- $vhost := trimPrefix "/" ($om.path | default "" | toString) -}}
{{- if not $vhost }}
{{- fail (printf "endpoints.oslo_messaging.path is empty for %s: with the RabbitMQ cluster-operator each component has its own vhost, never /" $serviceName) }}
{{- end }}
{{- $aux := $envAll.Values.conf.rabbitmq | default dict -}}
{{- range $k := keys $aux | sortAlpha }}
{{- if ne $k "policies" }}
{{- fail (printf "conf.rabbitmq.%s is set for %s: with the RabbitMQ cluster-operator only policies are carried over (as Policy resources); declare the rest as messaging-topology-operator resources" $k $serviceName) }}
{{- end }}
{{- end }}
{{- $ref := dict "name" $cluster -}}
{{- /* The umbrella writes the projection and passes its name (endpoints.oslo_messaging.operator.userSecret, #309 H2 run 4,
       option 4): then this chart imports it and renders none, so one writer owns it, and the umbrella creates this
       component only once it exists, so the User below is admitted at once. Standalone (no name passed) the chart
       renders it itself, and CDC retries the User until ESO has written it. */ -}}
{{- $passed := ($om.operator.userSecret | default "") | toString -}}
{{- $proj := $passed | default (printf "%s-rabbitmq-topology-user" $su) -}}
{{- $chartSecret := index ((($envAll.Values.secrets | default dict).oslo_messaging) | default dict) $serviceName | default "" -}}
{{- if eq (toString $chartSecret) $proj }}
{{- fail (printf "secrets.oslo_messaging.%s is %s, the name of the user projection the RabbitMQ topology resources import: one release cannot hold both (helm refuses a duplicate resource), so rename the chart's Secret" $serviceName $proj) }}
{{- end }}
{{- if not $passed }}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ $proj }}
  annotations:
    # Deleted by helm WITH the release, after its pre-delete hooks (#254).
    krateo.io/teardown: with-release
spec:
  # OnChange (#309 C): refreshed only when this ExternalSecret itself changes (a new root, a new
  # template), never on a timer, so a root edited by hand (which ESO regenerates) reaches no live
  # credential until a rotation; the umbrella reports the edit (status.credentialDrift).
  refreshPolicy: OnChange
  secretStoreRef:
    kind: SecretStore
    name: {{ $from.store | quote }}
  target:
    name: {{ $proj }}
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      metadata:
        labels:
          # The topology operator reads only Secrets with this label: its manager caches Secrets by it, and the User
          # webhook refuses an import Secret without it (messaging-topology-operator v1.20.5 cmd/main.go:281-283,
          # internal/webhook/v1beta1/label_validation.go). The same cache feeds the controller's Secret watch, so
          # without it a rotated root would not re-sync either. H2 run 3, 2026-11-18.
          rabbitmq.com/topology-operator: "true"
      data:
        username: {{ $auth.username | quote }}
        password: {{ "{{ .password }}" | quote }}
  data:
    - secretKey: password
      remoteRef:
        key: {{ $from.secret | quote }}
        property: {{ $from.key | quote }}
{{- end }}
---
{{- /* #309 C2: during a rotation the umbrella passes endpoints.oslo_messaging.operator.users, one {suffix, secret} per
       generation that must exist (the active one and the next, or the active one and the one still draining): one User
       and one Permission each, on the same vhost, so queues (which belong to the vhost) do not move. Without it, the
       one user of today. Names: <su>-rabbitmq-user<suffix> and <su>-rabbitmq-permission<suffix>; generation 0 has suffix
       "". The waits (helm-toolkit.utils.rabbitmq_topology_waits) name the ACTIVE generation (operator.activeSuffix). */ -}}
{{- $users := $om.operator.users | default list -}}
{{- if not $users -}}{{- $users = list (dict "suffix" "" "secret" $proj) -}}{{- end -}}
{{- range $u := $users }}
apiVersion: rabbitmq.com/v1beta1
kind: User
metadata:
  name: {{ printf "%s-rabbitmq-user%s" $su ($u.suffix | default "") }}
spec:
  rabbitmqClusterReference:
    name: {{ $cluster | quote }}
  importCredentialsSecret:
    name: {{ $u.secret }}
---
{{- end }}
apiVersion: rabbitmq.com/v1beta1
kind: Vhost
metadata:
  name: {{ printf "%s-rabbitmq-vhost" $su }}
spec:
  name: {{ $vhost | quote }}
  defaultQueueType: quorum
  deletionPolicy: retain
  rabbitmqClusterReference:
    name: {{ $cluster | quote }}
---
{{- range $u := $users }}
apiVersion: rabbitmq.com/v1beta1
kind: Permission
metadata:
  name: {{ printf "%s-rabbitmq-permission%s" $su ($u.suffix | default "") }}
spec:
  vhost: {{ $vhost | quote }}
  userReference:
    name: {{ printf "%s-rabbitmq-user%s" $su ($u.suffix | default "") }}
  permissions:
    configure: ".*"
    write: ".*"
    read: ".*"
  rabbitmqClusterReference:
    name: {{ $cluster | quote }}
---
{{- end }}
apiVersion: rabbitmq.com/v1beta1
kind: Permission
metadata:
  name: {{ printf "%s-rabbitmq-admin-permission" $su }}
spec:
  vhost: {{ $vhost | quote }}
  user: {{ (index ($om.auth | default dict) "admin" | default dict).username | quote }}
  permissions:
    configure: ".*"
    write: ".*"
    read: ".*"
  rabbitmqClusterReference:
    name: {{ $cluster | quote }}
{{- range $p := ($aux.policies | default list) }}
{{- $def := dict }}
{{- range $k, $v := ($p.definition | default dict) }}{{- if not (hasPrefix "ha-" $k) }}{{- $_ := set $def $k $v }}{{- end }}{{- end }}
---
apiVersion: rabbitmq.com/v1beta1
kind: Policy
metadata:
  name: {{ printf "%s-rabbitmq-policy-%s" $su ($p.name | lower | replace "_" "-") }}
spec:
  name: {{ $p.name | quote }}
  vhost: {{ $p.vhost | default $vhost | quote }}
  pattern: {{ $p.pattern | quote }}
  applyTo: {{ (index $p "apply-to") | default "all" | quote }}
  priority: {{ $p.priority | default 0 }}
  definition:
{{ toYaml $def | indent 4 }}
  rabbitmqClusterReference:
    name: {{ $cluster | quote }}
{{- end }}
{{- end -}}
