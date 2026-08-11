---
type: ExampleIndex
title: krateo-openstack-blueprint — examples
description: Index of the runnable manifests under examples/ — the one-Composition orchestrator, the identity plane per-component, and the compute plane per-component.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack
tags: [openstack, examples, orchestrator, identity, compute]
timestamp: 2026-08-11T00:00:00Z
---

# Examples

All example manifests live under [`examples/`](../examples). They deploy into the `openstack`
namespace (the `CompositionDefinition`s go in `openstack-system`).

## Runnable walkthrough

- [examples/identity-profile](../examples/identity-profile/README.md) — the recommended entry
  point: **one** `Openstack` orchestrator Composition (`profile: identity`) rolls out
  MariaDB+Memcached+Keystone+Glance+Horizon in dependency order, with verify steps.

## Manifests

- [examples/openstack.yaml](../examples/openstack.yaml) — a single `Openstack` orchestrator
  Composition. Set `spec.profile` (`identity` | `full`) or `spec.enabled` to an explicit
  component list. This is the manifest the walkthrough above uses.
- [examples/01-identity.yaml](../examples/01-identity.yaml) — the identity plane as one
  Composition per component (`Mariadb`, `Memcached`, `Keystone`, `Glance`, `Horizon`), which
  self-order via OpenStack-Helm's `kubernetes-entrypoint` dep-checks.
- [examples/02-compute.yaml](../examples/02-compute.yaml) — the compute plane per component
  (RabbitMQ, Placement, OVS, libvirt, Neutron, Nova) plus the extended catalog (Ironic,
  Cinder, Heat, Barbican, Designate, Octavia, Magnum, Manila, Mistral, Ceilometer, Aodh,
  Cloudkitty, Masakari, Trove, Watcher, Tacker, Cyborg, Blazar, Zaqar, Freezer, Skyline,
  Gnocchi, Swift). Requires the identity tier in the same namespace, an amd64 cluster with
  the OVS kernel module, and the compute node labels — see the repo-root `quickstart-gke.md`.

See [usage](./usage.md) for the two install paths and [configuration](./configuration.md) for
the value surface each Composition accepts.
