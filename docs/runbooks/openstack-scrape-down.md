---
type: Runbook
title: "Runbook: an openstack-monitoring scrape is down"
description: What to check when an openstack-scrape-down-<job> alert fires (the collector could not scrape a source, so the alerts on its metrics see no data).
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-monitoring
tags: [openstack, monitoring, runbook, opentelemetry]
timestamp: 2026-11-17T00:00:00Z
---

# An openstack-monitoring scrape is down

**Alerts:** `openstack-scrape-down-openstack-exporter`, `-openstack-mysqld-exporter`, `-openstack-rabbitmq`
(namespace `krateo-system`), one per scrape job.

**What fired:** the prometheus receiver's own `up` for that job (`service.name` = the job) stayed below 1 for two
consecutive 5-minute windows, or no point arrived at all. Every alert on that job's metrics is blind meanwhile:
HyperDX reads a window with no data as 0, which fires a "below" alert and silences an "above" one.

## Check, in this order

1. **Is every companion firing?** Then the collector, or its route to the Krateo collector, is what stopped:
   `kubectl -n openstack logs deploy/openstack-monitoring-collector`. Its default endpoint is the Krateo node agent's
   node-local Service, which drops traffic from a node that runs no agent (a tainted node, for instance).
2. **One job:** that target's pod. `openstack-exporter` exits when it cannot enable a single service (Keystone down, or
   its user's password refused). `openstack-mysqld-exporter` waits in Init until MariaDB answers.
   RabbitMQ answers on port 15692 of each server pod.
3. **The collector's log** names the scrape error per target.

## Do not

- Silence the companion and keep the other alerts: without their scrape they cannot fire.
