<p align="center">
  <img src="docs/krateo-loves-openstack.png" alt="Krateo loves OpenStack" width="720"/>
</p>

# Krateo Blueprints — OpenStack

## What is this

A set of [Krateo](https://krateo.io) blueprints that install OpenStack on Kubernetes using
the upstream [OpenStack-Helm](https://opendev.org/openstack/openstack-helm) charts.
**OpenStack-Helm ships one Helm chart per service**, so this repo provides **one blueprint
per chart** — each a `CompositionDefinition` whose Compositions install that single
component. "One OpenStack installation" is a *set* of Compositions deployed into one
namespace.

Every cross-component reference is *static configuration* (a fixed in-cluster Service DNS
name plus shared static passwords) and ordering is automatic across compositions (each
pod/job gates on its dependencies via `kubernetes-entrypoint` init-containers that wait on
Services/Jobs *by name*). So separate blueprints are correct — see
[docs/overview.md](docs/overview.md) for the full rationale.

There is a real **ordering** dependency (Keystone must be Ready before Glance/Nova/Neutron
register), and creating all Compositions at once races. So on top of the per-component
blueprints there is an **orchestrator umbrella** blueprint (`blueprints/openstack`, Kind
`Openstack`): one `Openstack` Composition registers every component `CompositionDefinition`,
then emits each component `Composition` only once its CRD exists **and** its dependency
Compositions report `Ready=True` (Helm `lookup`, re-evaluated every reconcile). One
`Openstack` Composition therefore rolls out a whole install in dependency order.

Each blueprint is **self-contained**: `blueprints/<c>/chart/` vendors the OpenStack-Helm
chart (plus `helm-toolkit`), pinned to release **2025.1 "Epoxy"** (`ubuntu_jammy` images),
with the Helm-hook job annotations stripped so Krateo's composition-dynamic-controller can
install them as plain, self-ordered resources. A curated `values.schema.json` drives each
Composition CRD.

**What's verified** (details in [docs/overview.md](docs/overview.md)):

- **Identity plane** — fully working on kind and GKE: MariaDB + Memcached + Keystone
  (+ Glance + Horizon); `openstack token issue` and the Horizon UI login both succeed.
- **Compute plane** — fully working on an amd64 cluster with the OVS kernel module (e.g.
  Ubuntu GKE nodes): OVS datapath, Neutron (ML2/OVS, VXLAN), Nova, libvirt/QEMU; a CirrOS VM
  boots to `ACTIVE`. Not possible on kind/Apple-Silicon — use GKE.
- **Bare-metal plane (`ironic`)** — production-grade blueprint (ipmi/redfish, iPXE/TFTP/HTTP,
  conductor on `hostNetwork`); provisioning a real node needs real hardware. Pairs with the
  [`openstack-ironic-operator-kog`](https://github.com/krateo-blueprints/openstack-ironic-operator-kog)
  KOG operator.

## Install

### Recommended: one orchestrator Composition

```sh
# Every component chart pins its pods to a node label, and nothing in the charts creates those
# labels. It is NOT one label: how many you need depends on which components you select.
#
#   openstack-control-plane=enabled   34 of the 36 charts -- every API, database, queue,
#                                     scheduler and conductor. BOTH profiles need it.
#   openstack-compute-node=enabled    the hypervisor: nova-compute, libvirt, plus
#                                     ceilometer-compute and cyborg-agent.   | profile: full
#   openvswitch=enabled               the data plane: the openvswitch        | only
#                                     DaemonSet and neutron's ovs agent.     |
#   openstack-network-node=enabled    octavia's worker, health-manager and   |
#                                     driver-agent.                          |
#
# profile: identity -- one label is enough:
kubectl label --overwrite nodes --all openstack-control-plane=enabled

# profile: full-strict -- three, not four: octavia is the only consumer of
# openstack-network-node and full-strict does not install it. Asserted against the rendered
# chart by hack/validate-node-label-preconditions.py, so this stays true or CI says so.

# profile: full -- all four. The --all form below is the safe one, and if you separate the
# roles by labelling individual nodes, ONE PAIR IS NOT SEPARABLE:
#
#   EVERY node carrying openstack-control-plane=enabled must ALSO carry openvswitch=enabled.
#
# Not a preference. neutron's dhcp, l3 and metadata agents are scheduled by
# openstack-control-plane, and each one refuses to start until a neutron-ovs-agent pod is Ready
# ON ITS OWN NODE (kubernetes-entrypoint's requireSameNode). The ovs agent is scheduled by
# openvswitch. So a node with the first label and not the second runs three agents that wait
# forever for a pod that will never be scheduled beside them -- in Init:0/2, which is neither
# Pending nor CrashLoopBackOff, so a pod census shows "19 in Init" and names nothing, and
# neutron's Composition reports "3 of 73 managed children are not ready" indefinitely.
#
# Measured: a 4-node GKE install of profile=full with openstack-control-plane on a 4-node
# default pool and openvswitch on a separate 2-node pool. DISJOINT -- so the requirement was
# unsatisfiable for all twelve agent pods, not merely some. hack/node-colocation.yaml declares
# the three pairs and hack/validate-same-node-colocation.py fails CI if a fourth appears
# undeclared.
#
# AND THE CONSEQUENCE IS ABOUT THE NODE IMAGE, NOT JUST THE LABELS: openvswitch needs the
# openvswitch kernel module, which Container-Optimized OS does not ship. So on GKE, every node
# you label openstack-control-plane=enabled must be UBUNTU_CONTAINERD when neutron is selected.
# Putting openvswitch on a separate Ubuntu pool because COS cannot run it is a correct decision
# against an incomplete precondition -- which is exactly how that cluster came to be built.
#
# (The compute label is what decides which nodes run VMs, and it IS separable.)
kubectl label --overwrite nodes --all openstack-control-plane=enabled \
  openstack-compute-node=enabled openvswitch=enabled openstack-network-node=enabled

# The orchestrator CHECKS this for the components you selected, and refuses to render -- naming
# the label, the components that want it and this command, on the Composition's own status --
# when it can see the cluster's nodes and none of them carries one. It is a mechanism and not
# just this paragraph because it was just this paragraph: the line above has been in this README,
# with the scheduler's message quoted next to it, and a fresh GKE install still stalled with 5
# pods Pending, because the scheduler says
#
#   0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector
#
# which names neither the label nor the chart that wants it. It was found by diffing a working
# cluster's node labels against the broken one's.
#
# Pending is the visible half. A DaemonSet whose nodeSelector matches no node creates NO POD AT
# ALL -- desiredNumberScheduled=0, nothing Pending, no event. A 4-node profile=full install whose
# nodes carried only openstack-control-plane ran 227 pods and had no nova-compute, no libvirt and
# no openvswitch: an OpenStack that could not boot an instance, reported by nothing.

# Register only the orchestrator blueprint...
kubectl create namespace openstack-system
# The blueprint release this path has been verified against. Pinned, not `latest`, for
# the same reason core-provider is pinned below: `latest` is whatever was cut most
# recently, which is not the same as whatever works. 0.3.10 through 0.3.12 could not
# complete a fresh keystone install at all, and for the hours they were `latest` this
# document handed them to anyone who followed it. Bump this line when a newer release
# has actually been run through these steps.
#
# DO NOT USE 0.3.29, 0.3.30 or 0.3.31. Their umbrella emits at most ONE component and
# then reports Ready=True forever: a right-trim above the YAML document separator glued
# it onto the previous component, so any render carrying two or more components failed
# and the release froze at revision 1. Fixed in 0.3.32 (issue #152).
#
# DO NOT INSTALL OR UPGRADE TO 0.3.33 THROUGH 0.3.42. 0.3.33 prunes every component CR on
# upgrade (#166). 0.3.36 and 0.3.37 upgrade the umbrella but leave every component at its old
# version (#179). 0.3.38 through 0.3.40 cannot upgrade a Job whose template changed (#179).
# 0.3.41 and 0.3.42 leave a cinder whose install hooks wait for Ceph forever, silently (#204);
# the 0.3.42 release notes say how to recover an install that has it.
#
# What 0.3.43 has actually been run through, stated so you know what is NOT covered:
#   - a fresh install with these exact steps, profile: full-strict, on a 6-node GKE cluster
#     (n2-standard-16, Kubernetes 1.36.4, core-provider 2.13.72). 32 components; all 33 Helm
#     releases deployed; the last component Ready 22 min after the apply; every Job a bounded
#     hook; keystone answering on /v3 (#204);
#   - an upgrade of a 0.3.34 install to 0.3.42 with hack/migrate-to-pvc.py, then on to 0.3.43:
#     MariaDB and RabbitMQ data intact across the upgrade, a node move and a RabbitMQ outage,
#     and a designate zone round-trip through PowerDNS (#179, #204).
# Six components are NOT Ready on that install, each with its own issue: nova, neutron and
# cyborg (#200), tacker (#201), magnum (#202), watcher (#203). cinder is not in full-strict
# from 0.3.43 because its only backend needs Ceph (#196). NOT verified at this version: the
# 36-component profile: full, kind, and the per-component path further down.

BP_VERSION=0.3.43

kubectl apply -f https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/${BP_VERSION}/compositiondefinition-openstack.yaml

# ...and let the orchestrator read node labels, so the check above can actually run. Krateo's
# composition-dynamic-controller renders this chart under a service account whose generated role
# grants it CRDs and not nodes, so without this the orchestrator cannot see the labels and skips
# the check -- silently and deliberately, because a refused Helm `lookup` is not an empty one: it
# is a template error that would fail the render on every reconcile, and a release that stops
# rendering gets uninstalled. Applied by you, rather than taken by the chart, because it is a
# cluster-scoped read for every service account in the namespace.
#
#   kubectl get clusterrole krateo-openstack-node-reader   # is the check enforced on this cluster
#   kubectl -n openstack get cm openstack-node-label-preconditions -o yaml
#                                                          # was it measured, and what does it want
#
# Edit the namespace in its ClusterRoleBinding subject if you install anywhere but `openstack`.
#
# APPLY IT ONCE PER CLUSTER, NOT PER RELEASE. Its two objects are named
# krateo-openstack-node-reader with no version in them, and the chart looks for that same fixed
# name, so one application covers every later release and every upgrade. (Deliberately: a
# version-pinned grant of this kind is a known failure -- it wedges each new release's fresh
# service account with `nodes is forbidden` until someone grants it by hand, and here it would
# instead make every upgrade quietly stop checking.) Re-applying it is harmless.
kubectl apply -f https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/${BP_VERSION}/node-reader-rbac.yaml

# Makes the 37 CompositionDefinitions visible on the portal's Blueprints page, which shows a user
# only the definitions in namespaces where they may `list compositiondefinitions`. It grants that
# in `openstack-system` to the groups `admins` and `devs`, the portal's default
# `blueprints-page.readerGroups`; edit the subjects if your portal install overrides that value.
# Once per cluster. Published from 0.3.37 on: with an older BP_VERSION, take it from that tag.
kubectl apply -f https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/${BP_VERSION}/portal-visibility-rbac.yaml

# ...then one Composition rolls out a whole install in dependency order.
kubectl create namespace openstack
kubectl apply -f https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/${BP_VERSION}/example-openstack.yaml
```

`profile: identity` brings up MariaDB+Memcached+Keystone+Glance+Horizon; `profile: full`
brings up **all 36 components** (needs a large amd64 cluster — see `quickstart-gke.md`).

#### `profile: full-strict` — full, minus the four that need provisioning you must do yourself

> **Upgrading an existing `full-strict` install to 0.3.43 or later uninstalls its `cinder`
> Composition.** From 0.3.43 `cinder` is one of the components `full-strict` leaves out, because
> its only shipped volume backend is Ceph and nothing provides a Ceph cluster (#196). On a
> default install that cinder never worked: `cinder-volume` never started, and from 0.3.41 its
> `storage-init` hook held the release in `pending-install`. Measured on #179 and on a cold
> install (#204). If you have provisioned Ceph and use cinder, switch to `profile: full`, or
> keep `full-strict` and add `cinder` to `enabled:`, **before** you upgrade.

Four components — `cinder`, `ironic`, `octavia` and `swift` — each wait on infrastructure that no
Kubernetes cluster provides and that this blueprint does not create. They are in `profile: full`,
and they stay there. `profile: full-strict` is the same set **minus those four**:

```yaml
spec:
  profile: full-strict   # 32 components, none of which needs provisioning you must do yourself
```

It also needs one node label fewer — three rather than four, since octavia is the only consumer
of `openstack-network-node` (see the labelling block above).

| component | needs | symptom when it is absent |
|---|---|---|
| `cinder` | a Ceph cluster, plus the ConfigMap `ceph-etc` and the keyring Secrets `pvc-ceph-client-key` and `cinder-volume-rbd-keyring` in `openstack`, for the default `rbd1` backend | `cinder-storage-init` and `cinder-volume` stay in `Init` on `FailedMount` for `ceph-etc`; `cinder-api` and `cinder-scheduler` wait on `cinder-volume` |
| `ironic` | an interface named `ironic-pxe` on each conductor node — the bare-metal provisioning NIC | `ironic-conductor-init` exits 1 with `Could not find IP for pxe to bind to`; the pod sits in `Init:CrashLoopBackOff` |
| `octavia` | `lb-mgmt-net` plus a Neutron port per node, named `octavia-{worker,health-manager}-port-<hostname>` | the `get-port` init containers log `No Port found for octavia-worker-port-…` and never finish |
| `swift` | a StorageClass offering `ReadWriteMany` (upstream uses an `nfs-provisioner` default this repo does not ship) | `swift-ring-builder` and `swift-storage` stay `Pending`, `swift-proxy` stays in `Init` |

**Why the choice is yours and not ours.** Measured on a 4-node GKE install of `profile: full`:
six pods at 14 restarts in `Init` plus four `Pending`, none of which could ever become `Ready` on
that cluster. `Init:` is neither `Pending` nor `CrashLoopBackOff`, so a pod census reports
`21 in Init` and names nothing — the state is invisible to exactly the command an operator runs
first. That is the case `full-strict` exists for.

But on a cluster where Ceph, the PXE NIC, `lb-mgmt-net` and an RWX StorageClass *are* present,
those same components install and work, and dropping them out of `profile: full` would take
components away from an install that has them — a behaviour change arriving through a version
bump, which is the one thing a blueprint must not do. So `full` keeps all 36 and `full-strict` is
the opt-in. (cinder joined the list in 0.3.43, which is why the notice above names the one
behaviour change `full-strict` itself carries: a cinder that could not start is removed.)

An entry in that list is a statement about today and not a verdict, and designate is the proof:
it was flagged here until [#165](https://github.com/krateo-blueprints/krateo-openstack-blueprint/issues/165)
shipped a `powerdns` blueprint. designate's pool declares a `pdns4` target and reaches it at
Service `powerdns`; nothing created that Service, so `${POWERDNS_SERVICE_HOST}` was never
injected and `designate-manage pool update` died on `` Field `host' cannot be None ``. The fix
was to deploy the thing it needs rather than to document the need, and `full-strict` went up by
two — designate back in, and powerdns itself.

`hack/provisioning-preconditions.yaml` carries each one with a command you can run to check it,
and `hack/validate-provisioning-preconditions.py` asserts **both** halves by rendering the chart:
every flagged component is still selected by `full`, none is selected by `full-strict`, and each
declares what it needs, its symptom and that check. Neither profile can drift from the other
silently, and a precondition cannot describe a component that no profile distinguishes.

To take just one of them on top of `full-strict` — say you have the RWX StorageClass but no PXE
NIC — name it in `enabled:`, which overrides `profile` and resolves its dependency closure:

```yaml
enabled:
  - swift
```

#### The install unlocks over several reconciles, and that is normal

The orchestrator emits a component's Composition only once its CRD exists *and* its dependencies
report `Ready`. Both conditions are evaluated with Helm `lookup` at render time, so the **first**
render emits no component Compositions at all — the CompositionDefinitions in that same pass are
what create the CRDs. Later passes unlock mariadb and memcached, then keystone, then glance and
horizon.

On the path above that happens by itself: the orchestrator is a Composition, so Krateo's
composition-dynamic-controller re-reconciles it on its own interval and each pass unlocks the
next level with nobody re-running anything. Seeing fewer components than the profile lists for
the first minutes is the gate working, not a stall.

If instead you install the umbrella chart **directly with helm**, nothing re-renders it, so you
have to drive the passes yourself with repeated `helm upgrade` until the set stops growing. That
route works and needs the manual passes; the route above needs none. Worth knowing before
concluding the gate is broken — someone on the Krateo path re-running upgrades is doing
unnecessary work, and someone on the helm path who expects one shot will stop at zero
components.

### Or drive the per-component blueprints directly

```sh
# Same node labels as above. The five components below are the identity plane, so
# openstack-control-plane is the only label they schedule on; the compute-plane example at the
# end of this block needs openstack-compute-node and openvswitch as well.
#
# NOTE: on this path nothing checks. The precondition check lives in the orchestrator umbrella
# (see above), so driving the per-component blueprints directly puts the labels back on you --
# a component installed with no node able to run it goes Pending, or in the case of a DaemonSet
# produces no pod at all and no error.
kubectl label --overwrite nodes --all openstack-control-plane=enabled

kubectl create namespace openstack-system
for c in mariadb memcached keystone glance horizon; do
BP_VERSION=0.3.32   # this per-component path was last verified at 0.3.32, not since
  kubectl apply -f https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/${BP_VERSION}/compositiondefinition-$c.yaml; done
kubectl create namespace openstack
kubectl apply -f https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/${BP_VERSION}/example-01-identity.yaml
# kubectl apply -f https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/${BP_VERSION}/example-02-compute.yaml   # compute plane (GKE)
```

Full install paths and how to access OpenStack/Horizon: [docs/usage.md](docs/usage.md).

## swift needs a ReadWriteMany StorageClass

swift's ring is a shared volume across its pods, so its PVC asks for `ReadWriteMany`:

```yaml
# blueprints/swift/chart/values.yaml -- upstream's default, unchanged here
ring:
  shared_storage:
    storageClassName: nfs-provisioner
```

**That default is upstream openstack-helm's, byte-for-byte**, and it is reasonable where an
openstack-helm deployment runs its own NFS provisioner. It is not reasonable everywhere, and
nothing in this blueprint provides one. On a stock GKE cluster the PVC never binds:

```
PVC swift-shared-storage  accessModes=[ReadWriteMany]  storageClassName=nfs-provisioner
  ProvisioningFailed x42: storageclass.storage.k8s.io "nfs-provisioner" not found
-> 4 x swift-storage Pending, swift never Ready
```

GKE's default `standard-rwo` cannot be substituted: it is block storage and **cannot do RWX at
all**. The RWX-capable classes there are Filestore-backed and are not the default.

So **before selecting swift, make sure the cluster has an RWX StorageClass, and name it**:

```yaml
spec:
  componentValues:
    swift:
      ring:
        shared_storage:
          storageClassName: my-filestore-rwx    # whatever your platform calls it
```

It is a values key, so this needs no change to the chart and no divergence from upstream — only
the knowledge that the default assumes infrastructure your cluster may not have. Nothing checks
it: an unbound PVC leaves pods `Pending` with an event on the PVC, which is quieter than it
sounds if you are watching Composition readiness rather than individual pods.

**gnocchi no longer depends on this.** Its metric store briefly used swift for exactly the reason
swift looked attractive — it is a component this blueprint provides — and moved to redis once the
PVC proved unprovisionable here. If you select swift, you select it because you want object
storage.

## MariaDB and RabbitMQ need a default StorageClass

From 0.3.36 both keep their data on a PersistentVolumeClaim, so the cluster needs a default
StorageClass. Check with:

```bash
kubectl get storageclass
```

Without one the claims stay `Pending`, `mariadb-server-0` and `rabbitmq-rabbitmq-0` stay `Pending`
with them, and nothing that depends on them starts. To use a named class, or more capacity, set it
before the first install, because Kubernetes refuses a later change to a StatefulSet's claims:

```yaml
spec:
  componentValues:
    mariadb:
      volume:
        class_name: my-class
        size: 20Gi
    rabbitmq:
      volume:
        class_name: my-class
```

**Up to and including 0.3.35, both stored their data on the node**: MariaDB at `/tmp/mysql-data`
and RabbitMQ at `/var/lib/rabbitmq`. On those releases a pod that lands on another node does not
find its data. On kind, where a node's `/tmp` is tmpfs, a node restart destroyed every OpenStack
database and MariaDB never recovered ([#177](https://github.com/krateo-blueprints/krateo-openstack-blueprint/issues/177)).

### Upgrading from 0.3.35 or earlier: migrate first

**Do not apply a 0.3.36-or-later `CompositionDefinition` to an existing install until both
components are migrated, and make that release 0.3.38 or later:** 0.3.36 and 0.3.37 upgrade the
umbrella but leave every component at its old version (see "Components follow the umbrella's
version" below, #179). That path has been run end to end on GKE, 0.3.34 to 0.3.42 and then 0.3.43, with the data intact (#179, #204). Kubernetes refuses to add claims to an existing StatefulSet. When that happens
the controller does not roll back: it patches the rest of the chart and keeps retrying the
StatefulSet, with `Ready=Unknown`. A Helm hook cannot do the move either, because the controller
runs hooks after it has applied the change. Measured with composition-dynamic-controller 2.13.71
and 2.13.72 on GKE.

So run `hack/migrate-to-pvc.py` on either side of the upgrade:

```bash
CTX=my-cluster-context     # always explicit; the script refuses to use the current context

for c in mariadb rabbitmq; do
  hack/migrate-to-pvc.py prepare --context "$CTX" --namespace openstack --component "$c"           # plan
  hack/migrate-to-pvc.py prepare --context "$CTX" --namespace openstack --component "$c" --apply
done

# now apply the 0.3.38 (or later) CompositionDefinition, as in "Install" above

for c in mariadb rabbitmq; do
  hack/migrate-to-pvc.py finish --context "$CTX" --namespace openstack --component "$c" --apply
done
```

`prepare` sets `krateo.io/paused` on the component's Composition and stops the StatefulSet. It
then checks that the node path really holds state, which means `grastate.dat` for MariaDB and
`mnesia` for RabbitMQ, and copies it into the claim the new StatefulSet will use. It compares file
counts and bytes, and only then deletes the old StatefulSet. `finish` checks that the
component's own `CompositionDefinition` now renders a chart that claims the volume (0.3.36 or
later, `claimsSince` in `hack/durable-state.yaml`), removes the pause, and waits for the pod to
be Ready on the claim. The node path is left in place, so delete it yourself once you are satisfied.

OpenStack is down from `prepare` until `finish` completes. `prepare` refuses, and leaves the
component paused at 0 replicas rather than half-migrated, when:

- the StatefulSet has more than one replica, because each pod's data is on a different node;
- the pod was never scheduled, so nothing says which node holds the data;
- the node path has no state in it. That is the #177 failure, where the data was already gone.
  Copying the empty directory would turn a crash loop into an empty database that looks
  healthy, so the install needs reinstalling instead.

`finish` refuses while the component's `compositiondefinition` `spec.chart.version` is still
older than 0.3.36. If it stays there after the new umbrella definition is applied, the new
release is 0.3.36 or 0.3.37, which never move any component (#179); apply 0.3.38 or later.

It does not read the Composition's `krateo.io/composition-version` label. On an umbrella install
that label alternates between the version the Composition was created at and the new one, about
once a minute, because two writers use two API versions (measured upgrading 0.3.34 to 0.3.38,
#179). A check on it passes or refuses depending on the second it runs.

CI runs the whole sequence against each released 0.3.35 chart with real data in it
(`hack/smoke-migrate-to-pvc.sh`). It also checks that an emptied node path is refused.

## One install per cluster

**A second OpenStack blueprint cannot be installed onto a cluster that already has one**, in any
namespace. The install fails in about fifteen seconds:

```
Error: INSTALLATION FAILED: Unable to continue with install:
  ClusterRole "openstack-view" in namespace "" exists and cannot be imported into the
  current release: invalid ownership metadata; annotation validation error:
  key "meta.helm.sh/release-namespace" must equal "openstack2": current value is "openstack"
```

The charts themselves are namespace-agnostic — released into `testns`, every
`kubernetes-entrypoint` wait resolves to `testns:keystone-api`, `testns:mariadb` and so on, and no
chart reaches back into a hardcoded `openstack`. That is not what blocks a second install.
**Cluster-scoped objects are.** They are singletons on the cluster, Helm records the installing
namespace in their ownership annotations, and a second release can neither adopt nor overwrite
them. There are eight:

| object | from |
| ------ | ---- |
| `ClusterRole/openstack-view`, `-provision`, `-decommission`, `-compute-tier-guard` | this orchestrator's capability roles |
| `ClusterRole/nova-bootstrap`, `nova-cell-setup` and both `ClusterRoleBinding`s | upstream openstack-helm's nova chart |

Only the first four are ours. **Nova's are upstream's**, and this repo tracks upstream except
where upstream is demonstrably broken — an unnamespaced bootstrap role is an upstream design
choice, not a defect. So renaming our four would reduce the collision surface from eight objects
to four and change where the failure happens, without making a second install work. That is why
this is documented rather than fixed.

The failure message is accurate and names the conflicting object, which is the useful half. What
it does not say is that the limit is structural, so: **one OpenStack blueprint per cluster.** A
second environment needs a second cluster.

## Upgrading an existing install

Bumping the blueprint version on a running install is one `kubectl apply` of the new
`CompositionDefinition`, and the orchestrator rolls the components forward. Two things about
that upgrade are worth knowing before you do it, because both of them are silent.

### Components follow the umbrella's version

The orchestrator registers every component `CompositionDefinition` at its own chart version
(`.Chart.Version`, stamped from the tag), and that is the only input. **Up to 0.3.37 it was a
value, `chartVersion`, and that broke every upgrade.** Its `values.schema.json` default becomes
the generated CRD's default, and the API server writes a default into the object when the object
is created. So a Composition created at 0.3.34 held `spec.chartVersion: 0.3.34` as if someone had
typed it, and every later umbrella re-rendered its components at 0.3.34. Measured on GKE,
upgrading 0.3.34 to 0.3.36:
- the umbrella's Helm release moved to `openstack-0.3.36`;
- all 33 component definitions stayed at `spec.chart.version: 0.3.34`;
- the new `powerdns` was registered at 0.3.34, a chart that was never published, so it could
  never sync ([#179](https://github.com/krateo-blueprints/krateo-openstack-blueprint/issues/179)).

From 0.3.38 there is no such value, so a stored object cannot choose the version. A Composition
created by an earlier release still carries `spec.chartVersion`; it is accepted and ignored.
`hack/validate-component-version-follows-chart.py` renders the chart with exactly that stale
spec and fails unless every component lands at the umbrella's version. It also refuses any
release-stamped placeholder in a schema `default` anywhere in the repo, because a stamped default
is frozen the same way.

To see whether an install is affected, compare the two:

```sh
helm -n openstack list --filter '^openstack'                      # the umbrella's chart version
kubectl -n openstack get compositiondefinitions \
  -o custom-columns='NAME:.metadata.name,VER:.spec.chart.version'  # each component's
```

### Jobs run as Helm hooks, so an upgrade can change them

A Job's pod template is immutable, and Krateo's composition-dynamic-controller patches a plain
Job in place. **Up to 0.3.40 every Job was plain, so any release that changed one failed that
component's upgrade**, and an image bump is enough. Measured on GKE upgrading 0.3.39 to 0.3.40:
`cannot patch "powerdns-api-key-init" with kind Job: ... spec.template: Invalid value`, while
the Composition still reported `Synced=True` ([#179](https://github.com/krateo-blueprints/krateo-openstack-blueprint/issues/179),
[core-provider#403](https://github.com/krateo-platformops/core-provider/issues/403)).

Every Job is now a `post-install,post-upgrade` hook with `before-hook-creation`. Each upgrade
deletes the old Job and runs the new one, so db-sync and the keystone registrations re-run on
upgrade as OpenStack-Helm intends. Teardown Jobs (db-drop, and the clean Jobs that delete Glance
images or storage secrets) are `pre-delete` hooks instead, so they run only on uninstall. On
0.3.40 and earlier they ran at install as plain Jobs. Measured with core-provider 2.13.72:
- hooks run on install and on upgrade, and the upgraded Job runs the new template;
- the controller does not wait for workloads to be Ready before running them, so a pod that
  waits on a hook Job through kubernetes-entrypoint cannot deadlock it;
- a release that failed on a plain Job recovers by itself: the next version's hook replaces the
  plain Job;
- a single 420-second hook, and three 120-second hooks in sequence, completed with no timeout.

Helm runs hooks one at a time in weight order and waits for each. A Job therefore takes its
weight from its role, in bands of dependency depth: db-init and the keystone registrations,
then db-sync and ks-endpoints, then storage-init, then bootstrap. `hack/validate-job-hooks.py`
proves, per chart, that each hook sorts after every Job it waits on, directly or through the
pods behind a service it waits on. It fails on any Job that is not a hook.

Every hook Job is bounded by `spec.activeDeadlineSeconds`, 1800 seconds by default
(`helm3_hook_deadline_seconds` overrides it per chart; a positive `jobs.<name>.activeDeadlineSeconds`
overrides it per Job). Helm waits for each hook to complete, and the controller sets no limit on a
Helm operation, so an unbounded hook that can never finish holds its release in `pending-install`
forever with no condition on the Composition. That happened to cinder on a cold 0.3.42 install,
waiting on Ceph objects nothing creates (#204, #196). 1800s is about 2.7 times the slowest Job
measured anywhere (672s, on the #179 upgrade); on a cold install the slowest of 161 Jobs took 101s.
Measured with core-provider 2.13.72 on a hook that can never start: at the deadline the operation
fails with `job … failed: DeadlineExceeded`, the controller logs it and retries as an upgrade, once
per deadline. **The Composition still reports `Ready=True`/`Synced=True` meanwhile**
(core-provider#403), so read the Helm release status, not the conditions, when a component stalls.
`hack/validate-job-hooks.py` fails on any hook Job without a positive deadline.

Two costs, both measured:
- **Hooks re-run on every revision.** Krateo wrote two Helm revisions per upgrade, six seconds
  apart, so each hook ran twice. OpenStack-Helm Jobs are idempotent; the cost is time.
- **A controller restarted mid-operation re-runs every hook.** The release is left pending,
  and about five minutes later the next controller pod rolls it back and runs the operation
  again. That happens to plain Jobs too, but hooks make an operation longer.

On a cluster still on 0.3.40 or earlier, a hook Job of the same name replaces the plain Job on
the first upgrade. Nothing has to be deleted by hand.

### Horizon sessions move to the database: every Horizon user is logged out once

Horizon now keeps sessions in its MariaDB (`session_engine: django.contrib.sessions.backends.db`)
instead of memcached. That is what lets the OIDC back-channel logout receiver find and end a
user's sessions (`conf.horizon.backchannel_logout`, off by default, see
[docs/configuration.md](docs/configuration.md)). Sessions created under the cache engine are not
carried over. **The upgrade that changes the engine logs every Horizon user out once.** They
sign in again, and nothing else is lost. The `django_session` table already exists: the
`horizon-db-sync` hook's `manage.py migrate` has always created it, because
`django.contrib.sessions` is in Horizon's `INSTALLED_APPS`. To keep the old behaviour, and
forgo back-channel logout, set
`componentValues.horizon.conf.horizon.local_settings.config.session_engine` back to
`django.contrib.sessions.backends.cache`.

### A component keeps the API version it was created at

The composition API version is derived from the chart version (`0.3.30` → `v0-3-30`), so a
bump changes it. That alone is harmless: Helm matches an object on name, namespace, kind and
group, so a move between served versions patches it in place and keeps its UID.

What breaks a component is being left out of the manifest. Helm prunes it, the object is
re-created with a new UID, and Krateo derives the Helm release name from the UID. The component
then has **two** releases: the one whose Deployments are actually running, and a new one that
can never succeed, because its Deployments would have to change `spec.selector` on objects that
already exist under the other release's name and Kubernetes forbids that. Measured on a real
upgrade: `mariadb-92wd4czz` at revisions 4, 5 and 6, all `FAILED`, beside `mariadb-9c847c79`
revision 2 `DEPLOYED` and serving the database. The cause was
[#166](https://github.com/krateo-blueprints/krateo-openstack-blueprint/issues/166), and a
component that exists now stays in the manifest across a bump.

The orchestrator also **pins**, so that a component's reads and writes stay on one schema. A
component that already exists is emitted at the version it was *created* at, which Krateo records
write-once on the object as the annotation `krateo.io/composition-origin-version`:

```sh
kubectl -n openstack get mariadb mariadb \
  -o jsonpath='{.metadata.annotations.krateo\.io/composition-origin-version}'
# v0-3-28     <- created by 0.3.28, still emitted at v0-3-28 after later bumps
```

A component that does not exist yet is emitted at the current version. An existing component
with no stamp, or whose stamped version is no longer served, is emitted at the current version
if that is served, otherwise at the newest served one. Nothing is needed from you.

### The cost: a value you add later is silently dropped on a pinned component

A pinned component is read and written **through its origin version's schema**, and an older
schema is a narrower one. The apiserver **prunes** every field a schema does not declare — on
writes, and, counter-intuitively, on reads as well. Measured on Kubernetes 1.36, with one
object written through the newer endpoint and read back through both:

```text
GET /apis/composition.krateo.io/v0-3-30/.../mariadbs/mariadb   ->   spec: {a, b}
GET /apis/composition.krateo.io/v0-3-28/.../mariadbs/mariadb   ->   spec: {a}
```

Same object, same storage. `b` is still there; the older endpoint simply does not hand it over.

**Any `spec.componentValues.<name>` property this blueprint adds after a component's origin
version therefore does nothing on that component.** It is not rejected and it raises no event:
it is dropped on the way in, and the only symptom is a setting that has no effect. If a value
you set appears to be ignored, read the annotation above: a component whose
`composition-origin-version` is older than the release that introduced the value cannot see it.

The fix is to recreate that component rather than to configure it harder — delete its
Composition and let the orchestrator emit it fresh, which re-stamps the origin at the current
version and restores the whole value surface. Expect that component's workloads to be recreated.

### Hard incompatibility: do not approve instance migration on these components

The pin works only while the origin version is still served, and what keeps it served is that
writing an instance through that endpoint keeps marking the version as in use. Approving
**instance migration** rewrites every instance through the *new* endpoint instead, which
releases the old version for removal and takes the pin with it.

So, for as long as components are pinned, their `CompositionDefinition`s must **not** approve
migration:

| `spec.upgradePolicy` | Effect |
| -------------------- | ------ |
| `Paused`             | Safe. Migration never runs. |
| `Manual` *without* the `krateo.io/upgrade-to-version` annotation | Safe. |
| `Manual` *with* `krateo.io/upgrade-to-version: <new version>` | **Defeats the pin.** |
| `Automatic`, or unset | **Defeats the pin** — unset is treated as `Automatic`. |

If migration is approved, the pinned version becomes removable and the pin stops applying: the
next render emits the component as an unstamped one. Between the old version being removed and
that render, an apply can land at a version that is no longer served and fail with
`no matches for kind`.

**Read the row you are actually on.** The component `CompositionDefinition`s this orchestrator
registers (`templates/definitions.yaml`) set no `upgradePolicy`, which is the last row —
migration is approved, and the pin above therefore falls back to the current version. Measured
on a live cluster running 0.3.28: all 35 component definitions report `upgradePolicy:
Automatic`, and every `composition.krateo.io` CRD on it serves exactly one version, the current
one, because each bump migrates the instances forward and the retired version is then pruned.

So the pin protects an install whose component definitions do not approve migration, and does
nothing on one that does:

```sh
# inspect
kubectl -n openstack get compositiondefinitions \
  -o custom-columns='NAME:.metadata.name,VER:.spec.chart.version,POLICY:.spec.upgradePolicy'

# opt a component out of migration, which is what makes the pin effective for it
kubectl -n openstack patch compositiondefinition mariadb --type=merge \
  -p '{"spec":{"upgradePolicy":"Paused"}}'
```

Pausing migration has its own price, and it is the other side of the same coin: a paused
component's instances stay on their origin version forever, so the CRD keeps every version it
has ever served instead of retiring them. Decide it per component, not fleet-wide by reflex.

The mechanism is in
`blueprints/openstack/chart/templates/_helpers.tpl` (`osh.pinVersion`), asserted by
`hack/validate-origin-version-pin.py`, and tracked as
[krateo-platformops/core-provider#303](https://github.com/krateo-platformops/core-provider/issues/303).

## Configure

For anything between the profiles, set **`spec.enabled`** to an explicit component list — it
overrides `profile` and is expanded to its **transitive dependency closure**:

```yaml
apiVersion: composition.krateo.io/v0-3-8   # derived from the chart version (0.3.8 -> v0-3-8)
kind: Openstack
metadata:
  name: openstack
  namespace: openstack
spec:
  enabled:
    - heat            # -> heat + keystone + rabbitmq + mariadb + memcached
```

Per-component overrides go under `spec.componentValues.<name>`, and each one is deep-merged into
that component's chart values. Every component exposes **every top-level key of its chart's
`values.yaml`**: `conf`, `pod`, `manifests`, `endpoints`, `network`, `images`, and also `labels`
(node selectors, which decide placement), `bootstrap`, `extraObjects`, `jobs`, `secrets` and the
rest. The fields declared under them are typed (`images.pull_policy`, replica counts,
`manifests.*` switches as booleans, `labels.*.node_selector_{key,value}` as strings,
`extraObjects` as a list of manifests, `neutron.network.interface.tunnel`, Horizon
`network.dashboard.service.type`); any other key below them passes through **untyped**
(`x-kubernetes-preserve-unknown-fields`), so for example
`componentValues.keystone.conf.keystone.DEFAULT.debug: true`,
`componentValues.nova.pod.useHostNetwork.novncproxy: true` or
`componentValues.nova.labels.api_metadata.node_selector_value: enabled` reaches the chart. The
typing happens in each component's own Composition CRD: the umbrella keys `componentValues` by
component name and passes each entry through untouched, because embedding the component schemas
there would multiply the umbrella CRD's size by every version it still serves. Up to 0.3.44 every
component was narrower than this (nova exposed only `images`), and an override outside the
declared fields was pruned by the Composition CRD with only a client-side warning (#209). On a
component created before that, a newly exposed key can still be dropped: see "Any
`spec.componentValues.<name>` property this blueprint adds after a component's origin version"
below. The
orchestrator also carries a `serviceContract` v1 stanza and an optional `contractEmitter`.
The whole surface is documented in [docs/configuration.md](docs/configuration.md), and the
CRD/schema in [docs/api.md](docs/api.md).

## Examples

- [examples/identity-profile/](examples/identity-profile/README.md) — one `Openstack`
  Composition (`profile: identity`) rolling out the identity plane, with verify steps.
- [examples/openstack.yaml](examples/openstack.yaml) — a single orchestrator Composition
  (set `spec.profile` or `spec.enabled`).
- [examples/01-identity.yaml](examples/01-identity.yaml) — the identity plane per component.
- [examples/02-compute.yaml](examples/02-compute.yaml) — the compute plane per component plus
  the extended catalog (GKE).

Index: [docs/examples.md](docs/examples.md).

## Docs

- [docs/index.md](docs/index.md) — the doc bundle map.
- [docs/overview.md](docs/overview.md) — the design, the orchestrator readiness gate, the
  three planes, the service contract.
- [docs/usage.md](docs/usage.md) — install paths and access.
- [docs/configuration.md](docs/configuration.md) — the whole `Openstack` value surface.
- [docs/api.md](docs/api.md) — the `CompositionDefinition` CRD + the composition Kinds +
  `serviceContract` v1 schema.
- [docs/examples.md](docs/examples.md) — the runnable manifests.
- [docs/release.md](docs/release.md) — how a tag publishes every chart to GHCR.
- [docs/log.md](docs/log.md) — curated history.
- [docs/quickstart.md](docs/quickstart.md) — deploy the umbrella + install the kagent SME.
- [docs/contract-emitter.md](docs/contract-emitter.md) — runtime usage/health emission.
- [docs/kolla-ansible-audit-and-helm-plan.md](docs/kolla-ansible-audit-and-helm-plan.md) —
  the upstream audit grounding the distribution-layer design.
- [docs/medium-openstack-as-a-service.md](docs/medium-openstack-as-a-service.md) — the
  narrative write-up.
- [docs/llms.txt](docs/llms.txt) — the version-pinned LLM doc index.

The dashboard (Horizon) logs in as `admin` / `password` (domain `Default`):

| Login | Identity → Projects (admin) |
| ----- | --------------------------- |
| ![Horizon login](docs/horizon-login.png) | ![Horizon dashboard](docs/horizon-dashboard.png) |

## Develop & release

`.github/workflows/release-tag.yaml` packages every `blueprints/*/chart` and pushes each to
GHCR on a semver tag (`git tag 0.1.0 && git push origin 0.1.0` →
`oci://ghcr.io/krateo-blueprints/charts/<component>:0.1.0`).

`.github/workflows/lint.yaml` runs on every PR: it checks the umbrella `componentValues`
schema is in sync (`hack/gen-componentvalues-schema.py`), runs `helm lint` + a **determinism**
(two byte-identical renders) and **hook-free** gate on every chart, compiles and conformance-
tests the contract emitter, resolves the service-contract SSO deep-link, and runs the shared
`lint-docs` documentation-standard job. See [docs/release.md](docs/release.md).

## License

Apache License 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

Everything under `blueprints/<component>/chart/` except the `openstack` umbrella is vendored
from [openstack-helm](https://opendev.org/openstack/openstack-helm) and
[openstack-helm-infra](https://opendev.org/openstack/openstack-helm-infra), which are
Apache-2.0. 815 files under `blueprints/*/chart/templates/` carry their own Apache-2.0 header
and copyright line, and those headers are authoritative for those files — the `NOTICE` names
the upstream copyright holders it can but is explicitly not exhaustive. Keep the headers when
rebasing onto a newer upstream.

The umbrella chart, the CompositionDefinitions, the gates under `hack/` and `tools/`, and this
documentation are original work of this repository, under the same license.
