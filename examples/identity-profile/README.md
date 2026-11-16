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
apiVersion: composition.krateo.io/<served-version>   # e.g. v0-3-8 for chart 0.3.8
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

Follow [README.md, Install](../../README.md#install). Its `example-openstack.yaml` is this file,
stamped by the release with the `apiVersion` that release serves; `profile: identity` is its
default. The file here is the unstamped template (`apiVersion:
composition.krateo.io/COMPOSITION_API_VERSION`), so applying it as it is installs nothing.

The orchestrator emits each component `Composition` only once its dependencies report
`Ready=True`: `mariadb`+`memcached` → `keystone` → `glance`+`horizon`.

## Verify

```sh
kubectl -n openstack get compositions.composition.krateo.io
# The admin openrc is the keystone-keystone-admin Secret, generated per install (#228); envFrom
# reads it without writing the password into the Pod object.
kubectl -n openstack run osclient --rm -it --restart=Never \
  --image=quay.io/airshipit/openstack-client:2025.1-ubuntu_jammy \
  --overrides='{"spec":{"containers":[{"name":"osclient","image":"quay.io/airshipit/openstack-client:2025.1-ubuntu_jammy","stdin":true,"tty":true,"command":["openstack","token","issue"],"envFrom":[{"secretRef":{"name":"keystone-keystone-admin"}}]}]}}'
```

Horizon logs in as `admin` with the install's generated password (domain `Default`): `kubectl -n openstack get secret openstack-credential-identity-admin -o jsonpath='{.data.password}' | base64 -d` on its NodePort (`31000`).

## Next steps

- `profile: full` (or `spec.enabled: [...]`) brings up the compute plane — needs an amd64
  cluster with the OVS kernel module; see the repo-root `quickstart-gke.md`.
- Per-component overrides go under `spec.componentValues.<name>` — see
  [../../docs/configuration.md](../../docs/configuration.md).
- To drive the components individually instead, see [../01-identity.yaml](../01-identity.yaml)
  and [../02-compute.yaml](../02-compute.yaml).

> The `apiVersion` is derived from the chart version by replacing dots with dashes, so a
> patch bump moves it. Read the served value rather than hardcoding it:
> 
> ```sh
> kubectl get crd openstacks.composition.krateo.io \
>   -o jsonpath='{.spec.versions[?(@.served==true)].name}'
> ```
> 
> Or use the stamped Compositions published with each release:
> `https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/latest/download/example-openstack.yaml`
