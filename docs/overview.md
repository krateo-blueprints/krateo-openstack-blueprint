---
type: Architecture
title: krateo-openstack-blueprint — overview
description: The design — one blueprint per OpenStack-Helm chart, why not an umbrella chart, the orchestrator's readiness-gated rollout, the identity/compute/bare-metal planes, and the service-contract surface.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack
tags: [openstack, architecture, orchestrator, openstack-helm, service-contract]
timestamp: 2026-08-11T00:00:00Z
---

# Overview

This repo packages the whole OpenStack-Helm stack as Krateo blueprints. OpenStack-Helm
ships **one Helm chart per service**, so the repo provides **one blueprint per chart** —
each blueprint is a `CompositionDefinition` (`core.krateo.io/v1alpha1`) whose Compositions
install that single component. An OpenStack installation is a *set* of Compositions in one
namespace.

## Why one blueprint per chart (and not one umbrella chart)

The design rule: *put components in separate blueprints when they are separate Helm charts;
only merge them into an umbrella blueprint when one blueprint's input depends on another
blueprint's output.* The OpenStack-Helm charts were checked against that rule:

- **No input→output dependencies.** Every cross-component reference is *static
  configuration* — a fixed in-cluster Service DNS name (`keystone`, `mariadb`, `rabbitmq`,
  …) plus shared static passwords. No chart reads another's *runtime* output; the
  helm-toolkit `*_lookup` helpers are compile-time template functions over the static
  `endpoints` map, not Kubernetes runtime lookups.
- **Ordering is automatic across compositions.** OpenStack-Helm gates every pod/job on its
  dependencies via `kubernetes-entrypoint` init-containers that wait on Services/Jobs *by
  name*. Those names are fixed, so the checks resolve across separate Compositions in the
  same namespace (Glance waits for the `keystone-api` Service and `mariadb` regardless of
  which blueprint created them).
- **Shared secrets are shared *inputs*, not outputs.** The few cross-cutting secrets
  (MariaDB root, RabbitMQ admin, Keystone admin) are identical static defaults; to
  customise them you set the same value in each Composition.

So separate blueprints are correct here. An umbrella *chart* would only be required if
secrets became **generated** — then the passwords would be outputs the other components
consume, which *is* an input→output dependency.

## The orchestrator umbrella (`Openstack`)

There is a real **ordering** dependency (Keystone must be Ready before Glance/Nova/Neutron
register), and creating all Compositions at once races — a slow Keystone install trips
Krateo's `create-pending` guard. So on top of the per-component blueprints there is an
**orchestrator umbrella** blueprint, `blueprints/openstack` (Kind `Openstack`). Its chart
does **not** bundle the OpenStack charts; it sequences the per-component blueprints:

1. it **registers** every component `CompositionDefinition` (`installDefinitions: true`),
   then
2. it emits each component `Composition` only once its generated CRD exists **and** all its
   dependency Compositions report `Ready=True` (Helm `lookup`, re-evaluated every reconcile).

The orchestrator's controller re-reconciles, so each pass `lookup` sees more components
become Ready and emits the next — an ordered, readiness-gated rollout. One `Openstack`
Composition therefore rolls out a whole install in dependency order (verified end-to-end:
`mariadb`+`memcached` → `keystone` → `glance`+`horizon`, then `openstack token issue`).

Component selection is `spec.profile` (`identity` | `full`) or, overriding it,
`spec.enabled` — an explicit component list expanded to its **transitive dependency
closure** (e.g. `["heat"]` also brings up keystone, rabbitmq, mariadb and memcached), so
you deploy exactly what you ask for plus what it needs and nothing else.

## The three planes / what is verified

- **Identity plane — fully working, composition-driven** on both kind (amd64 emulation) and
  GKE: MariaDB + Memcached + Keystone (+ Glance + Horizon). `openstack token issue`,
  `endpoint/service/user/catalog list` all succeed; the Horizon UI logs in.
- **Compute plane — fully working, composition-driven** on an amd64 cluster with the Open
  vSwitch kernel module (e.g. Ubuntu GKE nodes): OVS datapath (`br-int`/`br-tun`), Neutron
  (ML2/OVS, VXLAN), Nova control services, and libvirt with **QEMU** software virtualization
  (no `/dev/kvm` needed). A CirrOS VM boots to `ACTIVE`. Compute is **not** possible on
  kind/Apple-Silicon (no `/dev/kvm`, arm64-only) — use GKE. Two determinism fixes were
  required for the Krateo CDC path (see [medium-openstack-as-a-service](./medium-openstack-as-a-service.md)).
- **Bare-metal plane (`ironic`) — production-grade blueprint, composition-driven**: real
  driver stack (`ipmi`/`redfish`, iPXE/PXE/TFTP/HTTP boot), conductor on `hostNetwork`,
  provisioning-network wiring. On a cloud-only cluster the API and Keystone `baremetal`
  catalog registration come up; **actually provisioning a node requires real hardware** on
  the provisioning L2 with the PXE NIC labelled `openstack-control-plane=enabled` plus BMCs.
  Pairs with the
  [`openstack-ironic-operator-kog`](https://github.com/krateo-blueprints/openstack-ironic-operator-kog)
  KOG operator.

## Determinism and hook-free rendering

Krateo's composition-dynamic-controller (CDC) installs each Composition's chart under a
least-privilege ServiceAccount and re-renders every reconcile. Two properties are therefore
enforced by CI on every blueprint chart:

- **Hook-free** — the Helm-hook job annotations are stripped, so the charts install as
  plain, self-ordered resources (the CDC SA cannot get/delete hook Jobs, so hooked charts
  never converge).
- **Deterministic** — two renders of the same chart must be byte-identical; any
  `randAlphaNum`/`uuidv4`/`now` reaching a manifest would make each reconcile diff and churn
  (e.g. gnocchi statsd UUIDs, memcache secret keys), so those are pinned.

See [release](./release.md) for how the lint job enforces both.

## Service contract

The orchestrator carries a `serviceContract` stanza (contract v1): identity federation
(Keycloak client, groups mapper, optional IdP broker), a mandatory role mapping (CMP roles →
native OpenStack roles), a capability catalog (each capability renders an aggregation-labeled
ClusterRole, and ABAC capabilities additionally render a Kyverno ClusterPolicy), an SSO
deep-link, and usage/health/tagging/ticketing declarations. The runtime side of the
usage/health declaration is the **contract emitter** — CronJobs polling the OpenStack APIs
and writing normalized rows to ClickHouse (see [contract-emitter](./contract-emitter.md)).
The full field-by-field surface is in [configuration](./configuration.md); the schema is in
[api](./api.md).
