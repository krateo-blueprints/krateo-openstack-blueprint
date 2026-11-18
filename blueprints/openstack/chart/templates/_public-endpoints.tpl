{{- /* Public endpoints (#311). Values: .Values.publicEndpoints (the user's), .Values.publicEndpointRoutes and
       .Values.publicEndpointComponents (properties of the charts). Objects: templates/public-endpoints.yaml. Component
       values: osh.publicEndpointValues, merged UNDER componentValues in templates/compositions.yaml.
       hack/validate-public-endpoints.py checks all of it against the charts. */ -}}

{{- /* The routes of the SELECTED components, each with its hostname, as a JSON array. Ordered as the table is, so the
       render is deterministic. */ -}}
{{- define "osh.publicRoutes" -}}
{{- $top := index . 0 -}}
{{- $sel := index . 1 -}}
{{- $pe := $top.Values.publicEndpoints | default dict -}}
{{- $out := list -}}
{{- range $r := $top.Values.publicEndpointRoutes | default list -}}
{{- if has $r.component $sel -}}
{{- $out = append $out (merge (dict "fqdn" (printf "%s.%s" $r.host $pe.baseDomain)) $r) -}}
{{- end -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{- /* The endpoints.<key> override that moves one public endpoint to its route's hostname, over HTTPS on 443.
       helm-toolkit's keystone_endpoint_uri_lookup drops :443 for https, so the catalog URL reads
       https://<host>.<baseDomain><path>. */ -}}
{{- define "osh.publicEndpointOverride" -}}
{{- $r := index . 0 -}}
{{- dict "host_fqdn_override" (dict "public" (dict "host" $r.fqdn)) "scheme" (dict "public" "https") "port" (dict $r.port (dict "public" 443)) | toJson -}}
{{- end -}}

{{- /* What publicEndpoints changes in one component's values, as JSON; empty when publicEndpoints is off, and then
       compositions.yaml merges nothing, so the component's spec is exactly what it was. */ -}}
{{- define "osh.publicEndpointValues" -}}
{{- $top := index . 0 -}}
{{- $name := index . 1 -}}
{{- $sel := index . 2 -}}
{{- $pe := $top.Values.publicEndpoints | default dict -}}
{{- if $pe.enabled -}}
{{- $out := dict -}}
{{- $routes := include "osh.publicRoutes" (list $top $sel) | fromJsonArray -}}
{{- $endpoints := dict -}}
{{- range $r := $routes -}}
{{- if eq $r.component $name -}}
{{- $_ := set $endpoints $r.endpoint (include "osh.publicEndpointOverride" (list $r) | fromJson) -}}
{{- end -}}
{{- end -}}
{{- $pc := index ($top.Values.publicEndpointComponents | default dict) $name | default dict -}}
{{- range $key := $pc.referencedEndpoints | default list -}}
{{- range $r := $routes -}}
{{- if eq $r.endpoint $key -}}
{{- $_ := set $endpoints $key (include "osh.publicEndpointOverride" (list $r) | fromJson) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if $endpoints -}}{{- $_ := set $out "endpoints" $endpoints -}}{{- end -}}
{{- $manifests := dict -}}
{{- range $m := $pc.ingressManifests | default list -}}{{- $_ := set $manifests $m false -}}{{- end -}}
{{- if $manifests -}}{{- $_ := set $out "manifests" $manifests -}}{{- end -}}
{{- $conf := dict -}}
{{- with $pc.proxyHeaders -}}
{{- $_ := set $conf . (dict "oslo_middleware" (dict "enable_proxy_headers_parsing" true)) -}}
{{- end -}}
{{- if $pc.dashboard -}}
{{- $horizon := dict "local_settings" (dict "config" (dict
      "use_ssl" "True"
      "csrf_cookie_secure" "True"
      "session_cookie_secure" "True"
      "session_cookie_httponly" "True"
      "secure_proxy_ssl_header" true
      "csrf_trusted_origins" (list (printf "https://horizon.%s" $pe.baseDomain)))) -}}
{{- $_ := set $conf "horizon" $horizon -}}
{{- $_ := set $out "network" (dict "dashboard" (dict "service" (dict "type" "ClusterIP")) "node_port" (dict "enabled" false)) -}}
{{- end -}}
{{- if $conf -}}{{- $_ := set $out "conf" $conf -}}{{- end -}}
{{- if $out -}}{{- $out | toJson -}}{{- end -}}
{{- end -}}
{{- end -}}

{{- /* "true" on a live cluster. core-provider's own CRD is the witness, as in templates/credentials.yaml: where it is
       not served the render is offline (helm template, the gates) and nothing about the cluster can be checked. A named
       GET: the CRD list is fetched once per render in compositions.yaml, for the cost recorded on osh.crdExists. */ -}}
{{- define "osh.publicLiveCluster" -}}
{{- if lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" "compositiondefinitions.core.krateo.io" -}}true{{- end -}}
{{- end -}}

{{- /* "true" when the cluster serves the named CRD. A named GET. */ -}}
{{- define "osh.publicCrdServed" -}}
{{- if lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" (index . 0) -}}true{{- end -}}
{{- end -}}

{{- /* Refuses a publicEndpoints configuration that cannot work: a missing input, or (live) a cluster without the Gateway
       API or cert-manager. A refused render reports the reason on the Composition's status, like the ESO and node-label
       preconditions; rendering anyway would apply objects of kinds the cluster does not serve, which fails the apply on
       every reconcile with an error that names neither prerequisite. */ -}}
{{- define "osh.publicEndpointsRequire" -}}
{{- $top := index . 0 -}}
{{- $pe := $top.Values.publicEndpoints | default dict -}}
{{- if not $pe.baseDomain -}}
{{- fail "\n\npublicEndpoints.enabled is true but publicEndpoints.baseDomain is empty. Every public hostname is <service>.<baseDomain> (e.g. keystone.cloud.example.com): set it, or set publicEndpoints.enabled to false.\n" -}}
{{- end -}}
{{- if not ((($pe.tls | default dict).issuerRef | default dict).name) -}}
{{- fail "\n\npublicEndpoints.enabled is true but publicEndpoints.tls.issuerRef.name is empty. The Gateway's certificate is issued by cert-manager from that issuer (e.g. Krateo's krateo-selfsigned-ca ClusterIssuer, or an ACME issuer): name one.\n" -}}
{{- end -}}
{{- $np := $pe.networkPolicy | default dict -}}
{{- if and $np.enabled (not $np.nodeCIDRs) -}}
{{- fail "\n\npublicEndpoints.networkPolicy.enabled is true but publicEndpoints.networkPolicy.nodeCIDRs is empty. neutron's agents, nova-compute, libvirt and openvswitch run on the host network, so their traffic reaches RabbitMQ and the APIs from node addresses; a deny-by-default policy without them cuts the compute plane off its message bus. List the CIDRs the nodes' addresses come from, or set publicEndpoints.networkPolicy.enabled to false.\n" -}}
{{- end -}}
{{- if eq (include "osh.publicLiveCluster" (list $top)) "true" -}}
{{- $absent := list -}}
{{- range $n := list "gateways.gateway.networking.k8s.io" "httproutes.gateway.networking.k8s.io" "certificates.cert-manager.io" -}}
{{- if ne (include "osh.publicCrdServed" (list $n)) "true" -}}{{- $absent = append $absent $n -}}{{- end -}}
{{- end -}}
{{- if $absent -}}
{{- fail (printf "\n\npublicEndpoints.enabled is true but this cluster does not serve %s.\n\nThe public endpoints are a Gateway API Gateway with HTTPRoutes and a cert-manager Certificate. Install a Gateway API implementation (its CRDs and a controller for GatewayClass %q) and cert-manager -- Krateo's installer brings both with features.ingress -- and the next reconcile renders. Or set publicEndpoints.enabled to false. See the README's \"Public endpoints\".\n" (join ", " $absent) $pe.gatewayClassName) -}}
{{- end -}}
{{- end -}}
{{- end -}}
