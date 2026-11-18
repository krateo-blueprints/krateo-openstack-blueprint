{{/*
openstack-monitoring helpers (issue #312).
*/}}

{{/* "true" when component $name is in openstack.components (the umbrella's selection). */}}
{{- define "osm.selected" -}}
{{- $envAll := index . 0 -}}{{- $name := index . 1 -}}
{{- if has $name ($envAll.Values.openstack.components | default list) }}true{{ end -}}
{{- end -}}

{{/* openstack-exporter's service names (its --disable-service.<name> flags, v2.0.0-alpha main.go and exporters/exporter.go
     EnableExporter), by the component that serves each. A service whose component is not selected is disabled, so the
     exporter does not log a failure for it on every scrape. */}}
{{- define "osm.exporterServices" -}}
identity: keystone
image: glance
compute: nova
network: neutron
volume: cinder
placement: placement
object-store: swift
load-balancer: octavia
container-infra: magnum
dns: designate
baremetal: ironic
gnocchi: gnocchi
database: trove
orchestration: heat
sharev2: manila
{{- end -}}

{{/* The parsed alert source, files/alerts.yaml: the one place every metric and alert is defined. */}}
{{- define "osm.alertSource" -}}
{{- .Files.Get "files/alerts.yaml" -}}
{{- end -}}

{{/* "true" when the endpoints.<endpoint>.auth.<userClass> password comes from the umbrella's root (passwordFrom). */}}
{{- define "osm.passwordFrom" -}}
{{- $envAll := index . 0 -}}{{- $endpoint := index . 1 -}}{{- $userClass := index . 2 -}}
{{- with (index (index $envAll.Values.endpoints $endpoint).auth $userClass).passwordFrom }}{{ if .secret }}true{{ end }}{{ end -}}
{{- end -}}

{{/* openstack-exporter's clouds.yaml with $password as the password: a value, or an ESO template action. Rendered by
     the same helper in both modes, so the ExternalSecret holds exactly what the values-mode Secret would. */}}
{{- define "osm.cloudsYaml" -}}
{{- $envAll := index . 0 -}}{{- $password := index . 1 -}}
{{- $user := $envAll.Values.endpoints.identity.auth.openstack_exporter -}}
clouds:
  {{ $envAll.Values.openstack_exporter.cloud }}:
    auth:
      auth_url: {{ tuple "identity" "internal" "api" $envAll | include "helm-toolkit.endpoints.keystone_endpoint_uri_lookup" | quote }}
      username: {{ $user.username | quote }}
      password: "{{ $password }}"
      project_name: {{ $user.project_name | quote }}
      user_domain_name: {{ $user.user_domain_name | quote }}
      project_domain_name: {{ $user.project_domain_name | quote }}
    region_name: {{ $user.region_name | quote }}
    interface: {{ $envAll.Values.openstack_exporter.endpoint_type | quote }}
    identity_api_version: 3
{{ end -}}

{{/* mysqld-exporter's .my.cnf ([client], read through --config.my-cnf) with $password as the password. */}}
{{- define "osm.mysqldCnf" -}}
{{- $envAll := index . 0 -}}{{- $password := index . 1 -}}
{{- $auth := $envAll.Values.endpoints.oslo_db.auth.exporter -}}
[client]
user = {{ $auth.username }}
password = {{ $password }}
host = {{ tuple "oslo_db" "internal" $envAll | include "helm-toolkit.endpoints.hostname_fqdn_endpoint_lookup" }}
port = {{ tuple "oslo_db" "internal" "mysql" $envAll | include "helm-toolkit.endpoints.endpoint_port_lookup" }}
{{ end -}}

{{/* An ExternalSecret named $name whose target holds $data (an ESO template per key, {{ .password }} read from the
     root named by endpoints.<endpoint>.auth.<userClass>.passwordFrom), or, without passwordFrom, the Secret holding
     $values (the same keys, rendered with the password from values). (list $envAll $name $endpoint $userClass $data $values) */}}
{{- define "osm.credentialSecret" -}}
{{- $envAll := index . 0 -}}{{- $name := index . 1 -}}{{- $endpoint := index . 2 -}}{{- $userClass := index . 3 -}}
{{- $data := index . 4 -}}{{- $values := index . 5 -}}
{{- $from := (index (index $envAll.Values.endpoints $endpoint).auth $userClass).passwordFrom | default dict -}}
{{- if $from.secret }}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index $from $field) }}
{{- fail (printf "endpoints.%s.auth.%s.passwordFrom.%s is required" $endpoint $userClass $field) }}
{{- end }}
{{- end }}
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ $name }}
  annotations:
    # Deleted by helm WITH the release, not finalized ahead of the uninstall by composition-dynamic-controller (#254).
    krateo.io/teardown: with-release
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: SecretStore
    name: {{ $from.store | quote }}
  target:
    name: {{ $name }}
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      data:
{{- range $k, $v := $data }}
        {{ $k }}: {{ $v | quote }}
{{- end }}
  data:
    - secretKey: password
      remoteRef:
        key: {{ $from.secret | quote }}
        property: {{ $from.key | quote }}
{{- else }}
apiVersion: v1
kind: Secret
metadata:
  name: {{ $name }}
type: Opaque
data:
{{- range $k, $v := $values }}
  {{ $k }}: {{ $v | b64enc }}
{{- end }}
{{- end }}
{{- end -}}
