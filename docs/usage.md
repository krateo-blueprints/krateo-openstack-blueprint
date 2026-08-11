---
type: Usage
title: krateo-openstack-blueprint — usage
description: How to install — one orchestrator Composition (profile or explicit component set) or the per-component blueprints directly — plus accessing OpenStack from inside the cluster and the Horizon dashboard.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack
tags: [openstack, install, orchestrator, horizon, keystone]
timestamp: 2026-08-11T00:00:00Z
---

# Usage

Two ways to install: the recommended **one orchestrator Composition**, or the
**per-component blueprints** driven directly. Both need a Krateo control plane
(`krateoSupportedVersion: ">= 2.5.1"`) already installed.

## Recommended: one orchestrator Composition

Register the orchestrator blueprint, then apply one `Openstack` Composition — it registers
the component `CompositionDefinition`s itself and emits each component `Composition` in order
as its dependencies become Ready.

```sh
# Register only the orchestrator blueprint...
kubectl create namespace openstack-system
kubectl apply -f blueprints/openstack/compositiondefinition.yaml

# ...then one Composition rolls out a whole install in dependency order.
kubectl create namespace openstack
kubectl apply -f examples/openstack.yaml         # Kind: Openstack, spec.profile: identity | full
```

`profile: identity` brings up MariaDB + Memcached + Keystone + Glance + Horizon;
`profile: full` brings up the whole catalog (needs a large amd64 cluster — see
[quickstart](./quickstart.md) and `quickstart-gke.md`).

For anything in between, set **`spec.enabled`** to an explicit component list. It overrides
`profile` and is expanded to its **transitive dependency closure**, so you deploy exactly
what you ask for plus what it needs and nothing else:

```yaml
apiVersion: composition.krateo.io/v0-2-0
kind: Openstack
metadata:
  name: openstack
  namespace: openstack
spec:
  enabled:
    - heat            # -> heat + keystone + rabbitmq + mariadb + memcached
```

Per-component overrides go under `spec.componentValues.<name>` (strictly typed against each
component's chart schema). See [configuration](./configuration.md) for the whole surface.

## Or drive the per-component blueprints directly

Register the component `CompositionDefinition`s you want, then apply one Composition per
component — they self-order via OpenStack-Helm's `kubernetes-entrypoint` dep-checks.

```sh
kubectl create namespace openstack-system
for c in mariadb memcached keystone glance horizon; do
  kubectl apply -f blueprints/$c/compositiondefinition.yaml; done
kubectl create namespace openstack
kubectl apply -f examples/01-identity.yaml       # one Composition per component
# kubectl apply -f examples/02-compute.yaml      # compute plane (GKE)
```

Each Composition's `spec` is the curated chart values (e.g. `images.pull_policy`, replica
counts, `neutron.network.interface.tunnel`, `nova.conf.nova.libvirt.virt_type`). Empty
`spec: {}` uses the validated defaults.

## One namespace per installation

All the Compositions of one OpenStack installation live in a single namespace (the examples
use `openstack`). Cross-component references resolve by fixed in-cluster Service DNS name, so
every component must share the namespace. The `CompositionDefinition`s themselves live in
`openstack-system`.

## The dashboard (Horizon)

Horizon logs in as `admin` / `password` (domain `Default`); the Identity panels are
populated by Keystone. By default Horizon is exposed on a NodePort (`31000`); set
`componentValues.horizon.network.dashboard.service.type` to `LoadBalancer` for an external
IP on GKE/EKS/AKS (see [configuration](./configuration.md)).

## Accessing OpenStack (CLI)

From inside the cluster use the **internal** Keystone interface (`keystone-api:5000`):

```sh
kubectl -n openstack run osclient --rm -it --restart=Never \
  --image=quay.io/airshipit/openstack-client:2025.1-ubuntu_jammy \
  --env OS_AUTH_URL=http://keystone-api.openstack.svc.cluster.local:5000/v3 \
  --env OS_USERNAME=admin --env OS_PASSWORD=password --env OS_PROJECT_NAME=admin \
  --env OS_USER_DOMAIN_NAME=Default --env OS_PROJECT_DOMAIN_NAME=Default \
  --env OS_IDENTITY_API_VERSION=3 --env OS_REGION_NAME=RegionOne --env OS_INTERFACE=internal \
  --command -- openstack token issue
```

`openstack endpoint list`, `service list`, `user list`, `catalog list` all succeed once the
identity plane is Ready.

## Quickstarts

- [quickstart](./quickstart.md) — the happy path: deploy the umbrella `Openstack`
  Composition, reconfigure a component via `spec.componentValues`, then optionally install
  the `openstack-blueprint-expert` kagent SME.
- `quickstart-kind.md` (repo root) — identity plane on a local kind cluster.
- `quickstart-gke.md` (repo root) — disposable GKE cluster with the compute plane
  (Nova/QEMU, Neutron/OVS).
