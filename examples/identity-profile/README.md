---
type: Example
title: identity-profile — one Openstack Composition, identity plane
description: The recommended entry point — a single Openstack orchestrator Composition with profile identity that rolls out MariaDB+Memcached+Keystone+Glance+Horizon in dependency order.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack
tags: [example, openstack, orchestrator, identity]
timestamp: 2026-08-11T00:00:00Z
---

# identity-profile

The recommended entry point: **one** `Openstack` orchestrator Composition brings up the
identity plane (MariaDB + Memcached + Keystone + Glance + Horizon), rolled out in dependency
order. This is exactly [`examples/openstack.yaml`](../openstack.yaml) with the default
`profile: identity`.

## Manifest

```yaml
apiVersion: composition.krateo.io/v0-2-0
kind: Openstack
metadata:
  name: openstack
  namespace: openstack
spec:
  # identity = MariaDB+Memcached+Keystone+Glance+Horizon
  # full     = also RabbitMQ+Placement+OVS+libvirt+Neutron+Nova (compute; amd64 + OVS kernel module)
  profile: identity
```

## Run it

```sh
# 1. Register the orchestrator blueprint (it registers the component definitions itself).
kubectl create namespace openstack-system
kubectl apply -f ../../blueprints/openstack/compositiondefinition.yaml

# 2. One Composition rolls out the whole identity plane in dependency order.
kubectl create namespace openstack
kubectl apply -f ../openstack.yaml
```

The orchestrator emits each component `Composition` only once its dependencies report
`Ready=True`: `mariadb`+`memcached` → `keystone` → `glance`+`horizon`.

## Verify

```sh
kubectl -n openstack get compositions.composition.krateo.io
kubectl -n openstack run osclient --rm -it --restart=Never \
  --image=quay.io/airshipit/openstack-client:2025.1-ubuntu_jammy \
  --env OS_AUTH_URL=http://keystone-api.openstack.svc.cluster.local:5000/v3 \
  --env OS_USERNAME=admin --env OS_PASSWORD=password --env OS_PROJECT_NAME=admin \
  --env OS_USER_DOMAIN_NAME=Default --env OS_PROJECT_DOMAIN_NAME=Default \
  --env OS_IDENTITY_API_VERSION=3 --env OS_REGION_NAME=RegionOne --env OS_INTERFACE=internal \
  --command -- openstack token issue
```

Horizon logs in as `admin` / `password` (domain `Default`) on its NodePort (`31000`).

## Next steps

- `profile: full` (or `spec.enabled: [...]`) brings up the compute plane — needs an amd64
  cluster with the OVS kernel module; see the repo-root `quickstart-gke.md`.
- Per-component overrides go under `spec.componentValues.<name>` — see
  [../../docs/configuration.md](../../docs/configuration.md).
- To drive the components individually instead, see [../01-identity.yaml](../01-identity.yaml)
  and [../02-compute.yaml](../02-compute.yaml).
