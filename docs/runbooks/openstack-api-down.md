---
type: Runbook
title: "Runbook: an OpenStack API is down"
description: What to check when an openstack-api-down-<service> alert fires (openstack-exporter could not collect a single metric of that service).
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-monitoring
tags: [openstack, monitoring, runbook, api]
timestamp: 2026-11-17T00:00:00Z
---

# An OpenStack API is down

**Alerts:** `openstack-api-down-keystone`, `-glance`, `-nova`, `-neutron`, `-cinder`, `-placement`
(in the OpenStack release namespace).

**What fired:** `openstack_<service>_up` stayed below 1 for two consecutive 5-minute windows. openstack-exporter sets it
to 0 when every metric collection for that service failed, and the series is absent (HyperDX reads 0) when the exporter
could not even build a client for it, for instance because Keystone refused its login.

## Check, in this order

1. **Is it Keystone?** If `openstack-api-down-keystone` fires too, start there: every other API authenticates through
   it. `kubectl -n openstack get pods -l application=keystone` and the `keystone-api` logs.
2. **Is the scrape itself down?** If `openstack-scrape-down-openstack-exporter` fires, read
   [openstack-scrape-down](openstack-scrape-down.md) first: the exporter, not the API, may be what failed.
3. **The service's API pods:** `kubectl -n openstack get pods -l application=<service>,component=api`, their restarts,
   readiness and logs.
4. **Its dependencies:** MariaDB (`openstack-mariadb-down`), and for nova and neutron RabbitMQ.
5. **The exporter's view:** `kubectl -n openstack logs deploy/openstack-exporter` names the call that failed and the
   HTTP status it got.

## Do not

- Restart services that are not failing to clear the alert. The cause stays, and the logs that show it go with the pods.
- Delete the `openstack-credential-identity-user-openstack-exporter` Secret: it is the exporter user's only password
  (README "Monitoring").
