---
type: Runbook
title: "Runbook: MariaDB is unreachable"
description: What to check when openstack-mariadb-down fires (mysqld-exporter could not reach MariaDB).
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-monitoring
tags: [openstack, monitoring, runbook, mariadb]
timestamp: 2026-11-17T00:00:00Z
---

# MariaDB is unreachable

**Alert:** `openstack-mariadb-down` (in the OpenStack release namespace).

**What fired:** `mysql_up` stayed below 1 for two consecutive 5-minute windows: the standalone mysqld-exporter could not
log in to the `mariadb` Service as the `exporter` user and run its scrape.

## Check, in this order

1. **The server:** `kubectl -n openstack get pod mariadb-server-0`, its events and logs. A full volume stops it: check
   the `mysql-data` claim's usage.
2. **The exporter user:** if the server is up and every OpenStack service works, the exporter's login may be what
   fails: `kubectl -n openstack logs deploy/openstack-mysqld-exporter` shows the error MariaDB returned. The
   `mariadb-exporter-user` Job creates the user (`kubectl -n openstack logs job/mariadb-exporter-user`). It exists only
   when mariadb runs with `krateo.exporterUser`, which the umbrella's `monitoring` value sets. A standalone
   openstack-monitoring without it fires this alert, by design.
3. **The scrape:** `openstack-scrape-down-openstack-mysqld-exporter` firing as well means the exporter pod is down, not
   the database. See [openstack-scrape-down](openstack-scrape-down.md).

## Do not

- Delete the `mysql-data` PersistentVolumeClaim or the `mariadb-server-0` pod's volume: every OpenStack database is on it.
- Delete `openstack-credential-oslo-db-exporter` or any other `openstack-credential-*` Secret.
