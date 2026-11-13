{{/*
The user's home page (issue #247). Krateo blueprint addition, always on.

Horizon sends every user to the default dashboard's default panel, whether or not the deployment
runs what that panel needs: without compute (profile identity), the Project dashboard's Overview
requires openstack.services.compute and every login landed on 403. The package in
files/krateo_home/ adds GET /auth/home/, which redirects to the first dashboard whose default page
the user can open by Horizon's own checks, default dashboard first; HORIZON_CONFIG['user_home']
points there. With compute deployed the result is the page Horizon chose before.

Shipped like the back-channel receiver: the package in the horizon-bin ConfigMap, mounted at
/etc/openstack-dashboard/krateo_home/ and copied by horizon.sh into
openstack_dashboard/local/krateo_home/; the snippet in the horizon-etc Secret, linked into
local_settings.d, which Horizon executes after local_settings.
*/}}

{{- define "horizon.krateo_home.snippet_name" -}}
_80_krateo_home.py
{{- end -}}

{{- define "horizon.krateo_home.package_dir" -}}
/etc/openstack-dashboard/krateo_home
{{- end -}}

{{/* Horizon swallows exceptions raised by a snippet (it logs and continues), so
     hack/validate-horizon-home.py executes this text. WEBROOT gets its trailing slash only after
     the snippets run (openstack_dashboard/settings.py), hence the rstrip. */}}
{{- define "horizon.krateo_home.settings" -}}
# Rendered by the horizon chart (#247). Executed by Horizon from local_settings.d, after local_settings.
# Mounted by openstack_dashboard/urls.py under ^auth/: GET /auth/home/
AUTHENTICATION_URLS = list(AUTHENTICATION_URLS) + ["openstack_dashboard.local.krateo_home.urls"]
HORIZON_CONFIG["user_home"] = WEBROOT.rstrip("/") + "/auth/home/"
{{- end -}}
