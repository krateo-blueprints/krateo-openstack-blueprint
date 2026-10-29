# Quickstart (kind) — OpenStack identity plane

Deploy the **identity-plane** OpenStack blueprints (MariaDB + Memcached + Keystone, optionally
Glance + Horizon) on a local [kind](https://kind.sigs.k8s.io/) cluster, driven by Krateo
Compositions. Compute (Nova/VMs) is **not** possible on kind — see [`quickstart-gke.md`](quickstart-gke.md).

> **Apple Silicon note.** OpenStack images are amd64-only; some are manifest *lists* that omit
> arm64, which an arm64 node refuses. We run on a normal arm64 kind node and pre-load the images
> as amd64 (`tools/kind-load-images.sh`); they then run under Rosetta/qemu emulation. On a native
> amd64 cluster, skip the pre-load step.

## 1. Cluster + Krateo

```sh
kind create cluster --config tools/kind-openstack.yaml
kubectl label --overwrite nodes --all openstack-control-plane=enabled

helm repo add krateo https://charts.krateo.io && helm repo update
helm upgrade --install core-provider krateo/core-provider \
  --version 1.0.0 -n krateo-system --create-namespace --wait

tools/kind-load-images.sh openstack         # Apple Silicon only
```

## 2. Deploy via the orchestrator (one Composition)

```sh
kubectl create namespace openstack-system
kubectl apply -f https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/latest/download/compositiondefinition-openstack.yaml
kubectl wait compositiondefinition/openstack -n openstack-system --for=condition=Ready --timeout=180s

kubectl create namespace openstack
kubectl apply -f examples/openstack.yaml          # Kind: Openstack, profile: identity
```

The orchestrator registers the component CompositionDefinitions and emits each component
Composition in dependency order (mariadb+memcached -> keystone -> glance+horizon), each gated on
the previous being Ready. Watch it roll out:

```sh
kubectl get compositiondefinitions -n openstack    # the components it registered
kubectl get openstackmariadb,openstackmemcached,openstackkeystone,openstackglance,openstackhorizon -n openstack
kubectl -n openstack rollout status deploy/keystone-api --timeout=600s
```

> Prefer the per-component path instead? Register `https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/latest/download/compositiondefinition-<c>.yaml` for
> mariadb, memcached, keystone, glance, horizon and `kubectl apply -f examples/01-identity.yaml`.

> If you re-publish a changed chart under the **same** version, restart the analyzers so the CDC
> RBAC/chart cache is refreshed:
> `kubectl rollout restart deploy/core-provider deploy/core-provider-chart-inspector -n krateo-system`

## 4. Verify

```sh
kubectl -n openstack run osclient --rm -it --restart=Never \
  --image=quay.io/airshipit/openstack-client:2025.1-ubuntu_jammy \
  --env OS_AUTH_URL=http://keystone-api.openstack.svc.cluster.local:5000/v3 \
  --env OS_USERNAME=admin --env OS_PASSWORD=password --env OS_PROJECT_NAME=admin \
  --env OS_USER_DOMAIN_NAME=Default --env OS_PROJECT_DOMAIN_NAME=Default \
  --env OS_IDENTITY_API_VERSION=3 --env OS_REGION_NAME=RegionOne --env OS_INTERFACE=internal \
  --command -- openstack token issue
```

A token table means identity is functional.

## 5. The dashboard (Horizon)

Horizon (deployed by the `horizon` blueprint / the orchestrator) is on NodePort `31000`
(`http://localhost:31000` with the kind `extraPortMappings` in `tools/kind-openstack.yaml`). Log
in as `admin` / `password`, domain `Default`:

| Login | Identity → Projects (logged in as admin) |
| ----- | ---------------------------------------- |
| ![Horizon login](docs/horizon-login.png) | ![Horizon dashboard](docs/horizon-dashboard.png) |

The Identity panels are populated by Keystone. (The default *Compute* panel shows "not authorized"
on the identity profile — Nova is part of the compute plane; see [`quickstart-gke.md`](quickstart-gke.md).)

## Teardown

```sh
kind delete cluster --name openstack
```
