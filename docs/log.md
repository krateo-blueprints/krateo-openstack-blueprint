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
