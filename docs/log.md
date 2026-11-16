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
