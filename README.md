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
# DO NOT UPGRADE AN EXISTING INSTALL TO 0.3.34 OR ANYTHING EARLIER. 0.3.34 fixes only the
# case where a component has an origin stamp and that version is still served; a component
# with no stamp, or whose origin version is no longer served, is still pruned. Fresh installs
# are fine. UPGRADING to 0.3.33 deletes every
# component Composition CR. The emission gate asked for a CRD serving the chart's NEW
# version, which no cluster serves at the instant the bump renders -- the
# CompositionDefinitions in that same pass are what create it -- so every component
# document left the manifest and helm pruned the live CRs. Their Helm releases survive
# with nothing reconciling them. Measured on a 6-node GKE install upgrading
# 0.3.32 -> 0.3.33: 6 CRs left for 37 releases, 27 running workloads orphaned, and three
# components re-created with new release names that could not patch their own live
# objects because spec.selector is immutable. See issue #166.
#
# What 0.3.34 and 0.3.35 have actually been run through: NO INSTALL AND NO UPGRADE. 0.3.35
# covers the two cases 0.3.34 still pruned, and adds a PowerDNS that starts, which the chart
# first shipped on main could not do. Those fixes are verified by
# hack/validate-upgrade-does-not-prune.py against eight synthetic cluster states and by
# hack/smoke-powerdns.py against the rendered pods, and the defect was measured on a real
# cluster -- but nobody has yet performed a successful umbrella upgrade with either release,
# and designate has not been run against this PowerDNS. 0.3.35 exists so that test can run;
# until it does, "upgrading works now" is a claim about a gate and not about a cluster.
# BP_VERSION below therefore stays at 0.3.32, which is the newest release that HAS been run
# through the steps in this document.
#
# What 0.3.32 has actually been run through, stated so you know what is NOT covered: the
# Composition path below, on kind, with a 7-component closure (gnocchi + skyline + zaqar
# and their dependencies). keystone issued a real token, skyline and zaqar served, the
# umbrella reached revision 2 with all seven component documents and exactly one Helm
# release per component. NOT verified at this version: the 35-component `profile: full`,
# the compute tier, and the direct-helm route further down.
BP_VERSION=0.3.32

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

# ...then one Composition rolls out a whole install in dependency order.
kubectl create namespace openstack
kubectl apply -f https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/${BP_VERSION}/example-openstack.yaml
```

`profile: identity` brings up MariaDB+Memcached+Keystone+Glance+Horizon; `profile: full`
brings up **all 36 components** (needs a large amd64 cluster — see `quickstart-gke.md`).

#### `profile: full-strict` — full, minus the three that need provisioning you must do yourself

Three components — `ironic`, `octavia` and `swift` — each wait on infrastructure that no
Kubernetes cluster provides and that this blueprint does not create. They are in `profile: full`,
and they stay there. `profile: full-strict` is the same set **minus those three**:

```yaml
spec:
  profile: full-strict   # 33 components, none of which needs provisioning you must do yourself
```

It also needs one node label fewer — three rather than four, since octavia is the only consumer
of `openstack-network-node` (see the labelling block above).

| component | needs | symptom when it is absent |
|---|---|---|
| `ironic` | an interface named `ironic-pxe` on each conductor node — the bare-metal provisioning NIC | `ironic-conductor-init` exits 1 with `Could not find IP for pxe to bind to`; the pod sits in `Init:CrashLoopBackOff` |
| `octavia` | `lb-mgmt-net` plus a Neutron port per node, named `octavia-{worker,health-manager}-port-<hostname>` | the `get-port` init containers log `No Port found for octavia-worker-port-…` and never finish |
| `swift` | a StorageClass offering `ReadWriteMany` (upstream uses an `nfs-provisioner` default this repo does not ship) | `swift-ring-builder` and `swift-storage` stay `Pending`, `swift-proxy` stays in `Init` |

**Why the choice is yours and not ours.** Measured on a 4-node GKE install of `profile: full`:
six pods at 14 restarts in `Init` plus four `Pending`, none of which could ever become `Ready` on
that cluster. `Init:` is neither `Pending` nor `CrashLoopBackOff`, so a pod census reports
`21 in Init` and names nothing — the state is invisible to exactly the command an operator runs
first. That is the case `full-strict` exists for.

But on a cluster where the PXE NIC, `lb-mgmt-net` and an RWX StorageClass *are* present, those
same components install and work, and dropping them out of `profile: full` would take components
away from an install that has them — a behaviour change arriving through a version bump, which is
the one thing a blueprint must not do. So `full` is unchanged at 35 and `full-strict` is the
opt-in.

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
BP_VERSION=0.3.32   # the release this path is verified against; see the note above
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

Per-component overrides go under `spec.componentValues.<name>` (strictly typed against each
component's chart schema — e.g. `images.pull_policy`, replica counts,
`neutron.network.interface.tunnel`, Horizon `network.dashboard.service.type`). The
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
