---
type: ChartRepo
title: krateo-openstack-blueprint — index
description: The map of the krateo-openstack-blueprint doc bundle — a set of Krateo blueprints (one CompositionDefinition per OpenStack-Helm chart) plus an orchestrator umbrella that rolls a whole OpenStack install out in dependency order.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack
tags: [openstack, blueprint, compositiondefinition, chartrepo, openstack-helm]
timestamp: 2026-08-11T00:00:00Z
---

# krateo-openstack-blueprint

A repository of [Krateo](https://krateo.io) blueprints that install OpenStack on
Kubernetes using the upstream [OpenStack-Helm](https://opendev.org/openstack/openstack-helm)
charts. OpenStack-Helm ships **one Helm chart per service**, so this repo provides
**one blueprint per chart** — each a `CompositionDefinition` whose Compositions install
that single component. "One OpenStack installation" is a *set* of Compositions deployed
into one namespace.

On top of the per-component blueprints there is an **orchestrator umbrella** blueprint
(`blueprints/openstack`, Kind `Openstack`): one `Openstack` Composition registers every
component `CompositionDefinition`, then emits each component `Composition` only once its
generated CRD exists **and** all its dependency Compositions report `Ready=True` — an
ordered, readiness-gated rollout that avoids the all-at-once race.

## The bundle (start here)

- [overview](./overview.md) — the design (one blueprint per chart, why not an umbrella
  chart), the orchestrator readiness gate, the three planes (identity / compute / bare-metal),
  and the service contract.
- [usage](./usage.md) — install the orchestrator or the per-component blueprints, pick a
  profile or an explicit component set, access OpenStack and Horizon.
- [configuration](./configuration.md) — the whole `Openstack` value surface: `profile`,
  `enabled`, `componentValues`, `serviceContract`, `contractEmitter`.
- [api](./api.md) — the `CompositionDefinition` CRD, the per-component composition Kinds,
  and the `Openstack` composition schema.
- [examples](./examples.md) — the runnable manifests under `examples/`.
- [release](./release.md) — how a tag publishes every chart to GHCR.
- [log](./log.md) — curated history.
- [llms.txt](./llms.txt) — the version-pinned index of this bundle.

## Repository layout

- `blueprints/<component>/` — one self-contained blueprint per OpenStack-Helm chart.
  `blueprints/<c>/chart/` vendors the upstream chart (plus `helm-toolkit`), pinned to
  release **2025.1 "Epoxy"** (`ubuntu_jammy` images) with the Helm-hook job annotations
  stripped so Krateo's composition-dynamic-controller can install them as plain,
  self-ordered resources; `blueprints/<c>/compositiondefinition.yaml` is the sibling
  registration.
- `blueprints/openstack/` — the orchestrator umbrella (Kind `Openstack`). Its chart does
  **not** bundle the OpenStack charts; it sequences the per-component blueprints. Carries
  the `serviceContract` surface and the optional `contractEmitter` CronJobs.
- `examples/` — one-Composition-per-component identity/compute manifests plus the single
  `Openstack` orchestrator manifest.
- `kagent/` — the `openstack-blueprint-expert` kagent SME (Agent CR + ModelConfig) that
  can read and drive an install.
- `hack/gen-componentvalues-schema.py` — regenerates the orchestrator's
  `componentValues` schema from every component chart schema (CI enforces it is in sync).
- `docs/` — this bundle plus the deep-dive references (see below).

## Deep-dive references

- [quickstart](./quickstart.md) — deploy the umbrella, reconfigure a component via
  `spec.componentValues`, then optionally install the kagent SME.
- [contract-emitter](./contract-emitter.md) — the runtime usage/health emission
  (CronJobs → ClickHouse) that backs the `serviceContract` declaration.
- [kolla-ansible-audit-and-helm-plan](./kolla-ansible-audit-and-helm-plan.md) — the
  upstream audit that grounds the distribution-layer design.
- [medium-openstack-as-a-service](./medium-openstack-as-a-service.md) — the narrative
  write-up, including the two determinism fixes required for the Krateo CDC path.
