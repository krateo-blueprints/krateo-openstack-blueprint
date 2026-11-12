{{/*
OIDC back-channel logout receiver (conf.horizon.backchannel_logout). Krateo blueprint addition.

The receiver is plain Python in files/krateo_backchannel/. It ships in the horizon-bin ConfigMap,
is mounted at /etc/openstack-dashboard/krateo_backchannel/ and copied by horizon.sh into
openstack_dashboard/local/krateo_backchannel/ before collectstatic. The settings snippet below
ships in the horizon-etc Secret and is linked into local_settings.d, which Horizon executes after
local_settings.
*/}}

{{- define "horizon.backchannel_logout.enabled" -}}
{{- if .Values.conf.horizon.backchannel_logout.enabled -}}
true
{{- end -}}
{{- end -}}

{{- define "horizon.backchannel_logout.snippet_name" -}}
_90_krateo_backchannel.py
{{- end -}}

{{- define "horizon.backchannel_logout.credentials_dir" -}}
/etc/openstack-dashboard/backchannel-service-auth
{{- end -}}

{{- define "horizon.backchannel_logout.package_dir" -}}
/etc/openstack-dashboard/krateo_backchannel
{{- end -}}

{{- define "horizon.backchannel_logout.ca_dir" -}}
/etc/openstack-dashboard/backchannel-ca
{{- end -}}

{{/* "true" when ca_bundle_ref names an object and a key; half a reference fails the render. */}}
{{- define "horizon.backchannel_logout.ca_enabled" -}}
{{- $ref := .Values.conf.horizon.backchannel_logout.ca_bundle_ref | default dict -}}
{{- $name := $ref.name | default "" | toString -}}
{{- $key := $ref.key | default "" | toString -}}
{{- if and $name $key -}}
{{- if not (has ($ref.kind | default "ConfigMap") (list "ConfigMap" "Secret")) -}}
{{- fail (printf "conf.horizon.backchannel_logout.ca_bundle_ref.kind is %q: it must be ConfigMap or Secret" ($ref.kind | toString)) -}}
{{- end -}}
true
{{- else if or $name $key -}}
{{- fail "conf.horizon.backchannel_logout.ca_bundle_ref needs both name and key: one without the other configures nothing" -}}
{{- end -}}
{{- end -}}

{{/* Refuse to render a receiver that could not work. */}}
{{- define "horizon.backchannel_logout.validate" -}}
{{- $bcl := .Values.conf.horizon.backchannel_logout -}}
{{- range $key := list "issuer" "audience" "jwks_url" "forward_url" }}
{{- if not (index $bcl $key) }}
{{- fail (printf "conf.horizon.backchannel_logout.%s is required when backchannel_logout.enabled is true" $key) }}
{{- end }}
{{- end }}
{{- if ne .Values.conf.horizon.local_settings.config.session_engine "django.contrib.sessions.backends.db" }}
{{- fail "conf.horizon.backchannel_logout needs conf.horizon.local_settings.config.session_engine: django.contrib.sessions.backends.db (the receiver finds sessions in Horizon's database)" }}
{{- end }}
{{- if $bcl.ca_file }}
{{- fail "conf.horizon.backchannel_logout.ca_file is not a setting: set ca_bundle_ref (kind, name, key) to the ConfigMap or Secret holding the CA certificate; the chart mounts it and points the receiver at it, so the path and the mount cannot disagree" }}
{{- end }}
{{- $_ := include "horizon.backchannel_logout.ca_enabled" . -}}
{{- if not (contains "logout=backchannel" $bcl.forward_url) }}
{{- fail "conf.horizon.backchannel_logout.forward_url must be Keystone's OIDCRedirectURI with ?logout=backchannel" }}
{{- end }}
{{- end -}}

{{/* The local_settings.d snippet. Horizon swallows exceptions raised by a snippet (it logs
     and continues), so hack/validate-horizon-backchannel-render.py executes this text. */}}
{{- define "horizon.backchannel_logout.settings" -}}
{{- include "horizon.backchannel_logout.validate" . -}}
{{- $bcl := .Values.conf.horizon.backchannel_logout -}}
{{- $short := tuple "dashboard" "internal" . | include "helm-toolkit.endpoints.hostname_short_endpoint_lookup" -}}
{{- $namespaced := tuple "dashboard" "internal" . | include "helm-toolkit.endpoints.hostname_namespaced_endpoint_lookup" -}}
{{- $hosts := list $short $namespaced (printf "%s.svc" $namespaced) (printf "%s.svc.%s" $namespaced .Values.endpoints.cluster_domain_suffix) -}}
{{- $caFile := "" -}}
{{- if include "horizon.backchannel_logout.ca_enabled" . -}}
{{- $caFile = printf "%s/%s" (include "horizon.backchannel_logout.ca_dir" .) $bcl.ca_bundle_ref.key -}}
{{- end -}}
{{- $cfg := dict
  "issuer" $bcl.issuer
  "audience" $bcl.audience
  "jwks_url" $bcl.jwks_url
  "forward_url" $bcl.forward_url
  "identity_provider" ($bcl.identity_provider | default "")
  "keystone_url" ($bcl.keystone_url | default "")
  "iat_skew_seconds" (int $bcl.iat_skew_seconds)
  "max_token_age_seconds" (int $bcl.max_token_age_seconds)
  "require_typ" (ne (toString $bcl.require_typ) "false")
  "timeout_seconds" (int $bcl.timeout_seconds)
  "jwks_cache_seconds" (int $bcl.jwks_cache_seconds)
  "ca_file" $caFile
  "service_credentials_dir" (include "horizon.backchannel_logout.credentials_dir" .)
-}}
# Rendered by the horizon chart from conf.horizon.backchannel_logout. Executed by Horizon from
# local_settings.d, after local_settings.
import json as _krateo_bcl_json

ADD_INSTALLED_APPS = list(ADD_INSTALLED_APPS) + ["openstack_dashboard.local.krateo_backchannel"]
# Mounted by openstack_dashboard/urls.py under ^auth/: POST /auth/backchannel-logout/
AUTHENTICATION_URLS = list(AUTHENTICATION_URLS) + ["openstack_dashboard.local.krateo_backchannel.urls"]
# Keycloak calls the receiver by its in-cluster Service name.
ALLOWED_HOSTS = list(ALLOWED_HOSTS) + {{ $hosts | toJson }}
KRATEO_BACKCHANNEL = _krateo_bcl_json.loads({{ $cfg | toJson | toJson }})
{{- end -}}
