---
type: Log
title: krateo-openstack-blueprint — log
description: Curated chronological history of krateo-openstack-blueprint — notable changes and decisions, not a generated changelog.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack
tags: [openstack, log, history]
timestamp: 2026-08-11T00:00:00Z
---

# Log

Curated history; release notes live in GitHub Releases.

## 2026-11

- **Per-service MariaDB users (0.3.68, #228).** All 28 services' own MariaDB users still logged in with
  OpenStack-Helm's published password. Each MariaDB username now has a generated root,
  `openstack-credential-oslo-db-<user>`, passed only to the chart that owns it. nova's three databases and octavia's
  persistence database are one account each, so they get one root at several keys (`extraPaths`). The connection URLs
  leave the base config for a drop-in, `87-db-connection.conf` (helm-toolkit `db_dropin`). `<svc>-db-user` and its
  siblings become ExternalSecrets. Horizon reads its password from a file, and skyline's `skyline.yaml` carries both
  roots. db-init now runs `ALTER USER ... IDENTIFIED BY` after `CREATE USER IF NOT EXISTS`, so a changed password
  reaches an existing user; that is to be proposed upstream. Diego accepted the rotation window (2026-11-16); the
  upgrade run measures it. Gates: check 20 no longer exempts the database sections, and 20b fails a mysql URL that logs
  in as a per-service user with a published password. New checks: 15b (the map `hack/db-users.yaml`), 22 (drop-in
  fidelity and delivery, least privilege included) and 23 (db-init and db-drop run against a stand-in SQLAlchemy).
  nova's cell mappings now store URL templates that each process formats from its own config, so `nova_api` holds no
  password: nova caches each cell's connections per process (CELL_CACHE), and a pod that started before db-sync
  rewrote a literal URL kept the old password until it exited (check 24). neutron's agents no longer receive the
  database URL at all; only the servers and database tools do (`hack/db-readers.yaml`, check 22).

- **ceilometer's notification URLs (#281).** Since 0.3.57 the notification agent logged in to seven RabbitMQ vhosts
  as the administrator with the published password, which the server refuses. The URLs are now derived from
  `endpoints.oslo_messaging` and `conf.notification_vhosts`. With a generated admin root they reach only
  ceilometer-notification, through a drop-in. Check 20b (`hack/validate-credentials.py`) fails any URL that logs in as
  an install-wide administrator with a published password. It also found nova's `nova-db-api-admin` and
  `nova-db-cell0-admin` and octavia's `octavia-persistence-db-admin`, Secrets nothing read that carried the
  published MariaDB root password; they are no longer rendered.

- **Contract emitter removed (0.3.68).** The CMP's single emitter of the `serviceContract`
  usage and health is `krateo-platformops/cmp-usage-collector`, which reads each OpenStack
  from the parent (Diego, 2026-11-16). An emitter in this chart would double-count usage next
  to it. `contractEmitter.enabled: false` stays accepted for older Compositions; anything else
  is refused (`hack/validate-removed-values.sh`). The deep-link resolution step, lost from CI
  on 2026-08-11 while the docs still claimed it, runs again.

## 2026-08

- **Docs standard adopted.** Added the OKF documentation bundle (this `docs/` set +
  `docs/llms.txt` + `examples/identity-profile/README.md`), rewrote `README.md` to the
  six-section shape, and wired the shared `lint-docs` job into `.github/workflows/lint.yaml`.
- **Dead-org sweep.** Eliminated the legacy dead-org references in favour of
  `krateo-blueprints` (part of the krateo-platformops/installer standardization).

## Earlier

- **Orchestrator umbrella.** Added `blueprints/openstack` (Kind `Openstack`): one Composition
  registers every component `CompositionDefinition` and emits each component `Composition`
  once its CRD exists and its dependencies report `Ready=True` — an ordered, readiness-gated
  rollout that avoids the all-at-once `create-pending` race. `spec.enabled` selects an
  explicit component set expanded to its transitive dependency closure.
- **Service contract + contract emitter.** The orchestrator carries a `serviceContract` v1
  stanza (identity federation, role mapping, capabilities → aggregation ClusterRoles / Kyverno
  ABAC policies, SSO deep-link, usage/health/tagging/ticketing). Added the runtime
  `contractEmitter` (CronJobs polling OpenStack → ClickHouse), default-off.
- **Compute + bare-metal planes.** Verified the compute plane (OVS/Neutron VXLAN, Nova on
  libvirt+QEMU, CirrOS VM to `ACTIVE`) on amd64 GKE, and shipped the production-grade `ironic`
  blueprint (ipmi/redfish, iPXE/TFTP/HTTP, conductor on `hostNetwork`). Two determinism fixes
  were required for the Krateo CDC path.
- **One blueprint per chart.** Established the design: one `CompositionDefinition` per
  OpenStack-Helm chart, each vendoring the upstream chart (2025.1 "Epoxy", `ubuntu_jammy`)
  with Helm hooks stripped and a curated `values.schema.json`. Identity plane verified
  end-to-end on kind and GKE.
