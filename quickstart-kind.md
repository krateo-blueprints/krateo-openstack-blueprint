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

# core-provider is installed from its OCI chart, not charts.krateo.io: that repo still
# serves chart 1.0.0 / appVersion 1.0.0, an old release published under the pre-rename
# org (krateoplatformops). On 1.0.0 a CompositionDefinition whose chart cannot be read
# reports nothing at all -- empty status, no conditions, no CRD -- so an install failure
# is indistinguishable from an install still working. Fixed from 2.13.39 onward.
#
# 2.13.41 is pinned because it is the version this path has actually been run on
# end to end. If you bump it, skip 2.13.42: it shipped a regression where a CRD
# version created after the process started was invisible to helm Upgrade for the
# lifetime of that process, so a cluster could not enable a feature or bump a
# component without restarting a cdc. 2.13.41 predates it; 2.13.43 fixes it.
#
# Do NOT bump to 2.13.45. An earlier version of this comment recommended it, for real fixes
# it carries (a stale status.managed entry after a chart renames a managed child, and a lost
# status write during a CompositionDefinition version migration). It also shipped a regression
# that wedges the exact path those fixes were for: a drain wait LISTs pods without the RBAC to
# do so, so a CD version migration hangs at Synced=False/ReconcileError forever while the
# cluster is already in the correct state.
#
# Its symptom names the wrong layer, which is why it is worth writing down:
#
#     Timeout: failed waiting for *v1.Pod Informer to sync
#
# The informer swallows the 403 in its retry loop, so the message sends you to cache sync and
# apiserver latency rather than to RBAC. `kubectl auth can-i list pods --as=<the SA>` is what
# actually answers it.
#
# A fresh INSTALL on 2.13.45 is unaffected; it is version BUMPS that wedge. That distinction
# matters here, because bumping a blueprint chart version migrates a CompositionDefinition
# version -- so this repo triggers the affected path on every single release.
#
# 2.13.46 fixes it. At the time of writing it is tagged but not yet pullable
# (`helm show chart oci://ghcr.io/krateo-platformops/charts/core-provider --version 2.13.46`
# does not answer), so this does not recommend it as available -- check before bumping.
#
# The pin stays at 2.13.41 regardless, because that is the version this quickstart has
# actually been run on end to end, which is the whole reason the pin is worth trusting. It
# predates both the 2.13.42 and 2.13.45 regressions.
OCI=oci://ghcr.io/krateo-platformops/charts
helm upgrade --install core-provider-crds $OCI/core-provider-crds \
  --version 2.13.41 -n krateo-system --create-namespace --wait
helm upgrade --install core-provider $OCI/core-provider \
  --version 2.13.41 -n krateo-system --wait

tools/kind-load-images.sh openstack         # Apple Silicon only
```

## 2. Deploy via the orchestrator (one Composition)

```sh
kubectl create namespace openstack-system
# The blueprint release this path has been verified against. Pinned, not `latest`, for
# the same reason core-provider is pinned below: `latest` is whatever was cut most
# recently, which is not the same as whatever works. 0.3.10 through 0.3.12 could not
# complete a fresh keystone install at all, and for the hours they were `latest` this
# document handed them to anyone who followed it. Bump this line when a newer release
# has actually been run through these steps.
BP_VERSION=0.3.16

kubectl apply -f https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/${BP_VERSION}/compositiondefinition-openstack.yaml
kubectl wait compositiondefinition/openstack -n openstack-system --for=condition=Ready --timeout=180s

kubectl create namespace openstack
# The in-tree examples carry a COMPOSITION_API_VERSION placeholder; the release publishes them
# stamped to the apiVersion that release actually serves.
kubectl apply -f https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/${BP_VERSION}/example-openstack.yaml
```

The orchestrator registers the component CompositionDefinitions and emits each component
Composition in dependency order (mariadb+memcached -> keystone -> glance+horizon), each gated on
the previous being Ready. Watch it roll out:

```sh
kubectl get compositiondefinitions -n openstack    # the components it registered
kubectl get openstackmariadb,openstackmemcached,openstackkeystone,openstackglance,openstackhorizon -n openstack
kubectl -n openstack rollout status deploy/keystone-api --timeout=600s
```

> Prefer the per-component path instead? Register `https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/0.3.16/compositiondefinition-<c>.yaml` for
> mariadb, memcached, keystone, glance, horizon and `kubectl apply -f https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/0.3.16/example-01-identity.yaml`.

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
