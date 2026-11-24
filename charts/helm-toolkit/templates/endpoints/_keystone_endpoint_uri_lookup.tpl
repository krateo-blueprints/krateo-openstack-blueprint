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
  This function helps resolve uri style endpoints. It will omit the port for
  http when 80 is used, and 443 in the case of https.
values: |
  endpoints:
    cluster_domain_suffix: cluster.local
    oslo_db:
      hosts:
        default: mariadb
      host_fqdn_override:
        default: null
      path: /dbname
      scheme: mysql+pymysql
      port:
        mysql:
          default: 3306
usage: |
  {{ tuple "oslo_db" "internal" "mysql" . | include "helm-toolkit.endpoints.keystone_endpoint_uri_lookup" }}
return: |
  mysql+pymysql://mariadb.default.svc.cluster.local:3306/dbname
*/}}

{{- define "helm-toolkit.endpoints.keystone_endpoint_uri_lookup" -}}
{{- $type := index . 0 -}}
{{- $endpoint := include "helm-toolkit.endpoints.served_endpoint" (list (index . 0) (index . 1) (index . 3)) -}}
{{- $port := index . 2 -}}
{{- $context := index . 3 -}}
{{- $endpointScheme := tuple $type $endpoint $port $context | include "helm-toolkit.endpoints.keystone_endpoint_scheme_lookup" }}
{{- $endpointHost := tuple $type $endpoint $context | include "helm-toolkit.endpoints.endpoint_host_lookup" }}
{{- $endpointPort := tuple $type $endpoint $port $context | include "helm-toolkit.endpoints.endpoint_port_lookup" }}
{{- $endpointPath := tuple $type $endpoint $port $context | include "helm-toolkit.endpoints.keystone_endpoint_path_lookup" }}
{{- if or ( and ( eq $endpointScheme "http" ) ( eq $endpointPort "80" ) ) ( and ( eq $endpointScheme "https" ) ( eq $endpointPort "443" ) ) -}}
{{- printf "%s://%s%s" $endpointScheme $endpointHost $endpointPath -}}
{{- else -}}
{{- printf "%s://%s:%s%s" $endpointScheme $endpointHost $endpointPort $endpointPath -}}
{{- end -}}
{{- end -}}

{{/*
abstract: |
  The endpoint a URL is built for (#451). A `public` or `admin` endpoint nobody has given an address to falls back to the
  internal one: the charts' default public hosts (`heat`, port 80) name an ingress controller's Services, and nothing here
  serves them, so the catalog would advertise URLs that cannot answer. An address is given by
  host_fqdn_override.<endpoint> (what the umbrella's publicEndpoints sets) or host_fqdn_override.default, as a string or a map
  with a host (a map with only tls names none), or by hosts.<endpoint> as an IP address or a map.
usage: |
  {{ include "helm-toolkit.endpoints.served_endpoint" (list "orchestration" "public" .) }}
return: |
  internal
*/}}
{{- define "helm-toolkit.endpoints.served_endpoint" -}}
{{- $type := index . 0 -}}
{{- $endpoint := index . 1 -}}
{{- $context := index . 2 -}}
{{- $map := index $context.Values.endpoints ( $type | replace "-" "_" ) -}}
{{- $override := $map.host_fqdn_override | default dict -}}
{{- $given := false -}}
{{- range $key := list $endpoint "default" -}}
{{- $o := index $override $key -}}
{{- if kindIs "map" $o -}}{{- $o = $o.host -}}{{- end -}}
{{- if $o -}}{{- $given = true -}}{{- end -}}
{{- end -}}
{{- $host := index ($map.hosts | default dict) $endpoint -}}
{{- if or (kindIs "map" $host) (regexMatch "^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$" ($host | default "" | toString)) -}}{{- $given = true -}}{{- end -}}
{{- if or $given (not (has $endpoint (list "public" "admin"))) -}}
{{- $endpoint -}}
{{- else -}}
internal
{{- end -}}
{{- end -}}
