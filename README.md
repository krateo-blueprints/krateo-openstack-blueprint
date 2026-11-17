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

# External Secrets Operator generates every install's credentials, so they are the install's own
# instead of OpenStack-Helm's published defaults (#228; the Keystone admin password first, the
# others follow). From 0.3.52 the orchestrator REFUSES to render without ESO's CRDs, and says so on
# its status; installing it for an earlier release is harmless.
helm upgrade --install external-secrets external-secrets \
  --repo https://charts.external-secrets.io --version 2.14.0 \
  --namespace external-secrets --create-namespace --set installCRDs=true --wait

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
# the 0.3.42 release notes say how to recover an install that has it. 0.3.43 through 0.3.45
# publish broken Marketplace icons for 23 charts (#214); they install and run correctly.
#
# DO NOT INSTALL OR UPGRADE TO 0.3.65 (#228 slice 2b). Measured on GKE, upgrading from 0.3.64:
#   - watcher's decision engine crash-loops ("password is a required attribute"). watcher re-reads
#     its configuration without the drop-in directory that now holds its service password;
#   - ceilometer's upgrade never completes. Its db-sync Job does not mount that drop-in, so its
#     post-upgrade hook fails on every retry.
# The fix ships in 0.3.66. Stay on 0.3.64 until then.
#
# DO NOT INSTALL OR UPGRADE TO 0.3.66 either. Measured on GKE, upgrading from 0.3.64: barbican's db-sync Job
# crash-loops, so barbican never installs or upgrades. 0.3.66 passes --config-dir to every OpenStack program, and
# barbican-db-manage parses its own arguments, not oslo.config's, so it rejects that flag. Stay on 0.3.64; the fix
# ships in 0.3.67.
#
# DO NOT INSTALL OR UPGRADE TO 0.3.69 (#291). Measured on GKE, upgrading from 0.3.68: designate never
# becomes Ready. designate-worker's init container runs designate-manage without designate.conf.d, so it never
# reads the RabbitMQ connection 0.3.69 moved there and retries localhost forever. Stay on 0.3.68; the fix
# ships in 0.3.70.
#
# What 0.3.71 has actually been run through (2026-11-17), stated so you know what is NOT covered:
#   - 0.3.71 freshly installed with these exact steps, profile: full-strict, on a 3-node GKE cluster
#     (n2-standard-16, core-provider 2.13.107): all 32 components and the umbrella Ready 35 min after the
#     apply, with no intervention, although GKE resized the cluster's control plane mid-install (the
#     API server refused connections and every composition controller restarted; none was in a first
#     install at that moment). 253 pods, none Pending; containers restarted: mariadb-controller 3 times
#     (that API outage) and manila-bootstrap 7 times until nova-api was up (#299). Keystone rejects
#     every published password (51 users) and accepts the 34 generated ones; MariaDB accepts each of
#     the 25 per-service users with its generated password and refuses the published one, and counted
#     0 access-denied through the whole install; RabbitMQ accepts each of the 17 per-service users with
#     its generated password and refuses the published one. No published default remains in any of the
#     562 Secrets and ConfigMaps scanned (identity, MariaDB, RabbitMQ administrator and per-service).
#     glance holds exactly one copy of each bootstrap image, and its new image store requests 10 GiB;
#   - a fresh 0.3.68 install upgraded straight to 0.3.71 as "Upgrading an existing install" below
#     describes, instrumented (#228, #295): 32 components Ready 18 min after the upgrade started. No
#     failed Keystone login and no MariaDB access-denied after the upgrade. Each per-service RabbitMQ
#     user's password moves once, at this upgrade: every refusal came from a pod still holding the old
#     password before its component's final rabbit-init, or from a pod of the previous version. The
#     existing 2 GiB image store kept its size, with one copy of each image. A composition controller
#     killed mid-upgrade (SIGKILL inside horizon's db-sync hook) had its operation recovered within 1 s
#     of its restart. APIs failed 1-3 times each during their rollouts (#286). Its 0.3.68 base needed
#     one human step: GKE resized the control plane while cyborg was in its first install, and
#     core-provider 2.13.107 left cyborg deployed with its install hooks never run; deleting that
#     component's Composition recovered it in 4 min (core-provider#563, fixed in 2.13.108; #297);
#   - 0.3.68 freshly installed with these exact steps (External Secrets Operator 2.14.0 included),
#     profile: full-strict, on a 3-node GKE cluster (n2-standard-16, core-provider 2.13.106): all 32
#     components Ready 26 min after the apply, the umbrella 28 min; 254 pods, none Pending, and no
#     container restarted, during the install or in a 10-minute window after it (612 containers).
#     Keystone accepts every generated password and rejects OpenStack-Helm's published ones: the admin,
#     and all 34 service and test users the profile creates (the other 17 the charts can create are
#     absent, as their modes are off). MariaDB accepts each of the 25 per-service users the profile
#     creates with its generated password and refuses the published one (ERROR 1045); its access-denied
#     counter stayed at 0 through the whole install. nova's cell mappings hold URL templates, not
#     passwords; neutron's agents (15 pods) run with no database password; keystone-api reads its
#     database drop-in under Apache. Every ks-user and db-init Job succeeded. No published identity,
#     MariaDB or RabbitMQ-administrator password remains in any rendered Secret or ConfigMap (the
#     per-service RabbitMQ users are still published: 0.3.69);
#   - a fresh 0.3.67 install upgraded straight to 0.3.68, instrumented (#228): 32 components Ready 21 min
#     after the upgrade started. No failed Keystone login. Each per-service MariaDB user's password moves
#     once, at this upgrade: 24 connections were refused in all, every one by a pod still holding the old
#     password before it was replaced, the longest window gnocchi's (about 3 min, as it rolls out twice,
#     #277); none after the last replacement. The same user, database and leak checks as above. A
#     composition controller killed mid-upgrade (SIGKILL inside skyline's db-sync hook) had its operation
#     recovered 4 s after it restarted (core-provider #541, 2.13.106). API availability WAS measured:
#     keystone's token API failed for about 1-2 min twice, and placement, orchestration, metric and
#     workflow 1-3 times each, as each API runs one replica and its rollout drops it (#286);
#   - a fresh 0.3.51 install upgraded straight to 0.3.53 (the rotating upgrade): the admin
#     password moved off the published default, the admin user is exempt from failed-login lockout
#     (#252), and openstack-keystone-operator-kog 0.4.1 picked up the new password without a
#     restart and without a failed token request; a 0.3.53 install bumped to 0.3.54 was Ready in
#     4 min with no long pending upgrade (core-provider 2.13.94);
#   - a fresh 0.3.50 install upgraded to 0.3.51 as "Upgrading an existing install" below
#     describes, with no change to the Openstack: all 32 components at 0.3.51 5 min after the
#     apply, never fewer than 32 Ready during it; the previous version stopped being served 5 min
#     later. A Horizon user whose catalog has no compute lands on the Identity dashboard, not on
#     403 (#247);
#   - an identity-profile 0.3.49 install upgraded to 0.3.50 with no values change: every
#     component converged to the new version in 2.5 min (#233, core-provider 2.13.87); Keystone
#     WebSSO through an in-cluster Keycloak (keystone-oidc 2026.1): login, a federated user mapped
#     to its tenant project, and a Keycloak logout ending the Horizon session and revoking the
#     Keystone token the browser carried (#246);
#   - an upgrade of a 0.3.34 install to 0.3.42 with hack/migrate-to-pvc.py, then on to 0.3.43:
#     MariaDB and RabbitMQ data intact across the upgrade, a node move and a RabbitMQ outage,
#     and a designate zone round-trip through PowerDNS (#179, #204).
# Known, measured, not yet fixed:
#   - a component can read Ready while a pod of it crash-loops (#231);
#   - every API runs one replica, so an upgrade or config change drops it for 1-2 min (#286);
#   - on a fresh install ceilometer's notification agent starts before nova's RabbitMQ vhost exists, and
#     its worker crashes until nova's rabbit-init has run, then recovers on its own (#298);
#   - on a fresh install manila's bootstrap Job does not wait for the APIs it calls and fails until
#     nova-api is up, then succeeds (#299);
#   - if the API server goes away while a component is in its FIRST install (e.g. a managed control
#     plane resizing), core-provider up to 2.13.107 can leave it deployed with its install hooks never
#     run and its pods waiting forever. Recovery: delete that component's Composition, and the umbrella
#     re-creates it (core-provider#563; 2.13.108 marks it Ready=False/HooksMayNotHaveRun, #297).
# cinder is not in full-strict because its only backend needs Ceph (#196). NOT verified at this
# version: the 36-component profile: full, kind, a VNC console in a browser, and the
# per-component path further down.

BP_VERSION=0.3.71

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

# The capability roles (openstack-view, -provision, -decommission, -compute-tier-guard) and the
# compute-tier admission guard. Once per cluster, and BEFORE the first Openstack instance: the
# provision role is what lets a non-admin create that instance, and the guard has to exist to
# see that create. Published from 0.3.48 on; up to 0.3.47 the instance's own release rendered
# them, so with an older BP_VERSION skip this. Needs helm 3.17 or later for --take-ownership,
# which is what moves the objects over from a 0.3.47 install without removing them (see
# "Capability roles and guards" below). If you override serviceContract in the Openstack
# Composition, pass the same values here with -f so both render the same capabilities.
helm upgrade --install openstack-capabilities \
  oci://ghcr.io/krateo-blueprints/charts/openstack-capabilities --version ${BP_VERSION} \
  --namespace openstack-system --take-ownership

# Wait until the API server ACCEPTS this Composition, then apply it. core-provider creates the
# Openstack kind from the CompositionDefinition a few seconds later (28 s on a fresh GKE cluster),
# and applying before that fails with `ServiceUnavailable ... Kind=Openstack not found`. A server
# dry-run of the exact file is the test: it goes through the same handler, admission and schema as
# the apply. Discovery is not enough -- on a fresh cluster it listed the kind about a second before
# the API server would serve it, and that apply got the same 503 (#244). `kubectl wait` on the
# CompositionDefinition is version-blind: during an upgrade the previous version is still served.
# ...then one Composition rolls out a whole install in dependency order.
kubectl create namespace openstack
OS=https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/${BP_VERSION}/example-openstack.yaml
i=0; until kubectl apply --dry-run=server -f "$OS" >/dev/null 2>&1; do
  i=$((i+1)); [ $i -lt 60 ] || { echo "the API server has not accepted $OS after 5 min:"; kubectl apply --dry-run=server -f "$OS"; break; }; sleep 5; done
kubectl apply -f "$OS"
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
BP_VERSION=0.3.32   # this per-component path was last verified at 0.3.32, not since
for c in mariadb memcached keystone glance horizon; do
  kubectl apply -f https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/${BP_VERSION}/compositiondefinition-$c.yaml; done
# Wait until every kind the example uses is served at this version (see the wait above). The
# kinds come from the example itself, so this list cannot drift from what is applied.
kubectl create namespace openstack
EX=https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/${BP_VERSION}/example-01-identity.yaml
i=0; until kubectl apply --dry-run=server -f "$EX" >/dev/null 2>&1; do
  i=$((i+1)); [ $i -lt 60 ] || { echo "the API server has not accepted $EX after 5 min:"; kubectl apply --dry-run=server -f "$EX"; break; }; sleep 5; done
kubectl apply -f "$EX"
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
namespace. Up to 0.3.47 the install failed in about fifteen seconds on our own capability roles:

```
Error: INSTALLATION FAILED: Unable to continue with install:
  ClusterRole "openstack-view" in namespace "" exists and cannot be imported into the
  current release: invalid ownership metadata; annotation validation error:
  key "meta.helm.sh/release-namespace" must equal "openstack2": current value is "openstack"
```

From 0.3.48 those four roles come from the per-cluster chart (next section), so they no longer
collide. The conflict moves to upstream nova's objects, when a second install reaches nova.

The charts themselves are namespace-agnostic — released into `testns`, every
`kubernetes-entrypoint` wait resolves to `testns:keystone-api`, `testns:mariadb` and so on, and no
chart reaches back into a hardcoded `openstack`. That is not what blocks a second install.
**Cluster-scoped objects are.** They are singletons on the cluster, Helm records the installing
namespace in their ownership annotations, and a second release can neither adopt nor overwrite
them:

| object | from |
| ------ | ---- |
| `ClusterRole/nova-bootstrap`, `nova-cell-setup` and both `ClusterRoleBinding`s | upstream openstack-helm's nova chart |

Up to 0.3.47 the umbrella's four capability ClusterRoles were in this table too. **Nova's are
upstream's**, and this repo tracks upstream except where upstream is demonstrably broken — an
unnamespaced bootstrap role is an upstream design choice, not a defect. So moving our four out
changed where the failure happens without making a second install work. That is why this is
documented rather than fixed.

The failure message is accurate and names the conflicting object, which is the useful half. What
it does not say is that the limit is structural, so: **one OpenStack blueprint per cluster.** A
second environment needs a second cluster.

## Capability roles and guards

From 0.3.48 these objects come from the per-cluster chart `openstack-capabilities`:
- the four capability ClusterRoles (`openstack-view`, `-provision`, `-decommission`,
  `-compute-tier-guard`);
- the compute-tier `ValidatingAdmissionPolicy` and its binding.

An administrator installs the chart once per cluster, before any `Openstack` instance (the
`helm upgrade --install` in the install steps above).

Up to 0.3.47 the umbrella rendered them, and the umbrella is what an `Openstack` Composition
installs, so they came into existence only with the first instance:
- `openstack-provision` grants `create openstacks`, and did not exist until an `openstacks`
  object did, so it could never authorize a non-admin to create the instance;
- the compute-tier guard matches CREATE, and was installed by that create's own release, so it
  never saw the one create that mattered.

The chart takes the same `serviceContract` values as the umbrella. If you override the
capabilities in the Openstack Composition, pass the same values to the chart with `-f`. A file
carrying the Composition's whole spec is accepted, and its other keys are ignored.

Every one of these objects carries `helm.sh/resource-policy: keep`. Uninstalling the chart, or
the umbrella, leaves them in place, because a guard should not disappear as a side effect. To
remove them, delete them explicitly:

```sh
kubectl delete clusterrole openstack-view openstack-provision openstack-decommission openstack-compute-tier-guard
kubectl delete validatingadmissionpolicybinding,validatingadmissionpolicy openstack-compute-tier-guard
```

### Upgrading from 0.3.47 or earlier: the objects change owner, and are never absent

On an existing install the umbrella's release owns these objects. Upgrade in this order:

```sh
BP_VERSION=0.3.48   # or later
# 1. The per-cluster chart takes ownership in place. Without --take-ownership helm refuses with
#    an "invalid ownership metadata" error and changes nothing.
helm upgrade --install openstack-capabilities \
  oci://ghcr.io/krateo-blueprints/charts/openstack-capabilities --version ${BP_VERSION} \
  --namespace openstack-system --take-ownership
kubectl get clusterrole openstack-view \
  -o jsonpath='{.metadata.annotations.meta\.helm\.sh/release-name}{"\n"}'
#    -> openstack-capabilities
# 2. Then upgrade the blueprint as usual.
kubectl apply -f https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/${BP_VERSION}/compositiondefinition-openstack.yaml
```

Why neither step can remove them:
- **Step 1** updates the live objects in place. They now name `openstack-capabilities` as their
  owner and carry `keep`.
- **Step 2:** from 0.3.48 the umbrella re-renders a capability object only while its own release
  still owns the live copy. After step 1 it owns none, so it stops rendering them. Helm's upgrade
  deletes what a release stopped rendering, but it reads `keep` from the live object first and
  skips the delete.

Done the other way round, nothing is removed either. The 0.3.48 umbrella still owns the objects,
so it re-renders them unchanged apart from adding `keep`, and step 1 then takes them over.

The ownership decision is in `templates/_capabilities.tpl`. `hack/validate-capability-roles.py`
checks it against owned, foreign, absent and partly taken-over objects, and checks that the
umbrella renders none of them on its own.

## Upgrading an existing install

Bumping the blueprint version on a running install is one `kubectl apply` of the new
`CompositionDefinition`, and the orchestrator rolls the components forward. Two things about
that upgrade are worth knowing before you do it, because both of them are silent.

### Upgrading to 0.3.53 or later: install ESO first, and the Keystone admin password rotates

From 0.3.52 the orchestrator generates the Keystone admin password once per install, with an
External Secrets Operator `Password` generator, instead of using OpenStack-Helm's published
default (#228). Install ESO (the `helm upgrade --install external-secrets` step above) **before**
applying the new `CompositionDefinition`; without its CRDs the orchestrator refuses to render
and says why on its status, and the install stays on the previous release.

The upgrade rotates the password; nothing is left on the default:
- `openstack-credential-identity-admin` in the install's namespace holds the generated value. Never
  delete it: the generator would produce a new value no service accepts (it carries
  `helm.sh/resource-policy: keep` so an upgrade cannot remove it).
- Every `<component>-keystone-admin` Secret keeps its name and keys and is now written by an
  `ExternalSecret` from that root, so nothing has to be reconfigured. Measured on ESO 2.14.0:
  during the upgrade the name is absent for about 13 ms.
- **Readers outside the blueprint must pick up the new password.** openstack-keystone-operator-kog
  0.4.1 and later (`authBridge.openrcSecret: keystone-keystone-admin`) re-reads the Secret by itself:
  measured on a 0.3.51 → 0.3.53 rotation, it switched to the new credentials 63 s after ESO wrote
  the Secret, without a restart and without a single failed token request. Any other process that
  read the Secret only when it started (through `envFrom`), including that KOG before 0.4.1, keeps
  the old password and gets 401 until it restarts:
  `kubectl -n openstack rollout restart deployment <its deployment>`.
- **Use 0.3.53 or later, not 0.3.52.** keystone.conf locks an account for 30 minutes after five
  failed logins. On 0.3.52 such a stale reader locked the admin account for everyone, again every
  30 minutes, and component upgrades that needed it waited (measured: Glance's hooks waited 34
  minutes, and the correct new password got 401 while locked). From 0.3.53 the keystone chart
  exempts the admin user from lockout (`ignore_lockout_failure_attempts`, set by `db-sync` before
  `bootstrap` resets the password), so a stale reader gets 401 without locking anyone else out. The
  password is 32 random characters, so brute force is not what the lockout would stop; human and
  federated users keep it.
- Keystone's `db-sync` hook runs `keystone-manage bootstrap` with the new value, which resets the
  admin user's password without needing the old one.
- A rollback across this upgrade fails without changing anything and the next reconcile moves
  forward again; the generated password stays.

### Upgrading to 0.3.56 or later: the MariaDB root password rotates

From 0.3.56 the MariaDB root password is generated per install too (`openstack-credential-oslo-db-admin`,
the same ESO mechanism as the Keystone admin password above). On the upgrade that ships it:
- every component's `<component>-db-admin` Secret keeps its name and key (`DB_CONNECTION`), now
  written by an `ExternalSecret` with the generated password in the URL;
- the MariaDB server reads root from `mariadb-dbadmin-generated` and its client config from
  `mariadb-secrets-generated`, so its pod spec changes and the server restarts once. On that start
  MariaDB's own start script re-creates `root` with the new password (`CREATE OR REPLACE USER` from an
  init file), which needs no old password;
- anything outside the blueprint that connects as MariaDB root with the old password gets refused
  from then on and must read the new value from `openstack-credential-oslo-db-admin`. Never delete
  that Secret.

### Upgrading to 0.3.57 or later: RabbitMQ's passwords and Erlang cookie rotate

From 0.3.57 three more RabbitMQ credentials are generated per install, using the same ESO mechanism.
Each was OpenStack-Helm's published default:
- `openstack-credential-oslo-messaging-admin` and `openstack-credential-oslo-messaging-guest`, the
  passwords of RabbitMQ's two users. Both carry the `administrator` tag, and the guest user is not
  restricted to loopback (`loopback_users.guest: false`).
- `openstack-credential-oslo-messaging-erlang-cookie`, the Erlang distribution cookie. Whoever holds
  it gets a remote shell on the RabbitMQ node.

On the upgrade that ships it:
- every component's `<component>-rabbitmq-admin` Secret keeps its name and keys (`RABBITMQ_CONNECTION`,
  and `TRANSPORT_URL` where the chart has it). It is now written by an `ExternalSecret` with the
  generated password in each URL. The components' `rabbit-init` jobs read it to create their own
  RabbitMQ users;
- the RabbitMQ server reads both passwords from `rabbitmq-admin-user-generated` and its cookie from
  `rabbitmq-erlang-cookie-generated`, so its pod spec changes and the server restarts once. On that
  start its init container writes both users, with the new password hashes, into the definitions file
  the server loads at boot, which needs no old password;
- **with more than one RabbitMQ replica, use 0.3.58 or later, not 0.3.57.** Every node must hold the
  same cookie, and a rolling restart would start a node that its peers refuse, so the cookie only
  changes on a single node:
  - while more than one replica is wanted, the rabbitmq render is refused. The reason is written to
    the rabbitmq component's status (`ChartRenderable False`);
  - set `componentValues.rabbitmq.pod.replicas.server: 1`. The chart first scales to one node, still
    on the old cookie, then rotates the cookie on that node;
  - set the replicas back afterwards: new nodes join with the new cookie.

  On 0.3.57 the refusal also blocked the scale-down, so replicas 1 never took effect (measured: the
  StatefulSet stayed at 2 nodes). If you are stuck there, upgrade to 0.3.58. The blueprint's
  default is 1 replica, which is not affected;
- the cluster-wait Job and the test Pod read the admin connection URL from that Secret. Upstream puts
  it in their pod specs, password included, as a literal env value;
- anything outside the blueprint that logs in to RabbitMQ as `rabbitmq` or `guest` with the old
  password is refused from then on. It must read the new value from the matching root Secret. Never
  delete those Secrets.

### Upgrading to 0.3.58 or later: MariaDB's other users rotate

From 0.3.58 MariaDB's three other built-in users get generated passwords, using the same ESO mechanism
as root:
- `openstack-credential-oslo-db-audit`: the `audit` user can `SELECT` every database (Keystone's
  included) from any pod;
- `openstack-credential-oslo-db-sst`: the Galera state-transfer user. Its password was also written
  in plain text into the `mariadb-etc` ConfigMap (`wsrep_sst_auth`);
- `openstack-credential-oslo-db-exporter`: the mysqld-exporter user, which exists only with
  `monitoring.prometheus.enabled`.

On the upgrade that ships it:
- the MariaDB server reads them from `mariadb-dbsst-generated` and `mariadb-dbaudit-generated`, so its
  pod spec changes and it restarts once. On that start MariaDB's start script re-creates `sst` and
  `audit` with the new passwords (`CREATE OR REPLACE USER` from an init file);
- `wsrep_sst_auth` is no longer in the ConfigMap, in either mode. It is a drop-in,
  `/etc/mysql/conf.d/10-sst-auth.cnf`, taken from the sst Secret. If you override
  `conf.database.00_base` with your own `wsrep_sst_auth`, the drop-in is read after it and wins;
- with monitoring on, the exporter's create-user Job, an upgrade hook, re-creates the user from
  `mysql-exporter-generated`, and the exporter sidecar reads the same Secret;
- anything outside the blueprint that logs in as `audit` or `exporter` with the old password is
  refused from then on. Never delete the root Secrets.

0.3.58 also removes RabbitMQ's `guest` user. Upstream's definitions file declared it, an
administrator reachable from any pod, so RabbitMQ re-created it on every boot. It then lived until
the next component upgrade's rabbit-init deleted it. Measured on 0.3.56: created at boot, deleted
8 minutes later by the first rabbit-init. Nothing uses it, so the file no longer declares it.
`openstack-credential-oslo-messaging-guest`, generated by 0.3.57, is no longer read. It stays in
place; never delete it.

### Upgrading to 0.3.60 or later: each service's memcache secret key rotates

From 0.3.60 every service that validates tokens with keystonemiddleware gets its own generated
`memcache_secret_key`: `openstack-credential-memcache-<service>`, one per service, for 24 services.
keystonemiddleware caches token-validation results in memcached, which has no authentication and is
reachable from any pod. With `memcache_security_strategy = ENCRYPT` the entries are protected by
that key, so the published key let anyone forge a "this token is valid" entry for that service.

On the upgrade that ships it:
- the key is no longer in any service's rendered config. It arrives as a drop-in,
  `/etc/<service>/<service>.conf.d/80-memcache-secret-key.conf`, from an ExternalSecret
  `<service>-memcache-secret-key`. Swift's proxy now loads `/etc/swift/proxy-server.conf.d/`, whose
  `[filter:authtoken]` drop-in carries it;
- cloudkitty, cyborg, ironic and masakari now set `memcache_security_strategy = ENCRYPT`. They had
  no strategy, so their token cache was unprotected whatever the key;
- the pods that read a service's config restart once, because their spec gains the drop-in. Cached
  token validations made under the old key are simply missed and validated again.
- ceilometer and freezer declared a key but never used one, so nothing changes for them.

### Upgrading to 0.3.61 or later: the metadata proxy secret and Horizon's SECRET_KEY rotate

From 0.3.61 two shared secrets are generated per install:
- `openstack-credential-metadata-proxy-shared-secret`. Neutron's metadata agents sign each instance's
  metadata request with it, and nova-api-metadata trusts what verifies. With the published value
  (`password`), any pod could forge a metadata request for any instance and read its user-data, keys
  and vendor data. Nova (`[neutron]`) and every neutron metadata agent (`[DEFAULT]`, OVS or OVN) read
  it from a drop-in, `81-metadata-proxy.conf`, in their `conf.d` directory;
- `openstack-credential-horizon-secret-key`. Django signs Horizon's session cookies and CSRF tokens
  with it, so the published value let anyone forge a session. **Use 0.3.62 or later for this one.**
  0.3.61 delivered it as a `local_settings.d` snippet that Horizon never executes. With no key in
  `local_settings`, each Horizon pod generated its own random key in `/tmp`. Measured: the
  published key was no longer used, but neither was the generated one, and sessions broke on every
  pod restart and across replicas. From 0.3.62, `local_settings` reads the key from the mounted
  generated Secret (`/etc/openstack-dashboard/krateo-secret-key/secret_key`).

On the upgrade, **every Horizon user is signed out once**: sessions signed with the old key are
rejected. Nova's metadata API and the metadata agents restart with the new secret together, and
instances keep reading their metadata.

### Upgrading to 0.3.63 or later: the compute service users rotate

Every OpenStack service authenticates to Keystone as its own service user. In this blueprint each
consumer has its own Keystone username: nova reaches neutron as `nova_neutron`, cinder reaches nova
as `cinder_nova`. Each chart's ks-user Job creates those users. All of them used OpenStack-Helm's
published password.

From 0.3.63, 30 of them are generated per install, one `openstack-credential-identity-user-<username>`
each:
- nova's, neutron's, cinder's and placement's users, and the cross-service ones (`nova_neutron`,
  `neutron_nova`, `nova_cinder`, ...);
- the service-token users (`*_service_user`), blazar and zaqar;
- every chart's `test` user. That is the password in its openrc Secret: no Job creates test users in
  Keystone (they are for `helm test`).

**Upgrade to 0.3.64, not 0.3.63.** On 0.3.63:
- the ks-user Jobs set the new passwords **after** the upgrade (a post-upgrade hook);
- nova, neutron and placement never restarted. Their `<service>-ks-etc` Secret kept its name, and
  nothing in their pod templates changed with its content.

The processes kept the old password, failed authentication five times, and Keystone's failed-login
lockout locked `nova`, `neutron` and `placement` for 30 minutes. Their APIs answered 503 until the
lockout expired and the pods were restarted (measured on GKE,
[#228](https://github.com/krateo-blueprints/krateo-openstack-blueprint/issues/228)).

From 0.3.64:
- **each user's password changes in Keystone before its readers restart.** A user whose password comes
  from the umbrella is set by `<service>-ks-user`, a **pre-upgrade** hook. A pre-upgrade hook runs
  before the release's resources are applied, so that Job reads nothing the release renders:
  - its script is inline, not the previous revision's `<service>-bin`;
  - the user's fields come from values, and the password from the umbrella's Secret.

  Users still set from values keep OpenStack-Helm's post-upgrade Job, named
  `<service>-ks-user-values` when a chart has both kinds;
- **service users are exempt from the lockout.** That Job runs
  `openstack user set --ignore-lockout-failure-attempts` before it sets the password, so a pod still
  holding the old value fails without locking the user;
- **every reader restarts.** Each workload that reads `<service>-ks-etc` or the keystone openrc carries
  a hash of it in its pod template (`secret-ks-etc-hash`, `secret-keystone-hash`). The upgrade to
  0.3.64 restarts those 22 workloads of nova, cinder, neutron, placement, blazar, cloudkitty and zaqar
  once.

Cyborg reads `nova` and `placement` from its `cyborg-identity-*` drop-ins. Five usernames are read by
more than one chart: `nova`, `placement` (cyborg), `glance`, `neutron-test` and `test`.

If an install is already on 0.3.63 with locked users, upgrade to 0.3.64. Its Job exempts every
rotated user, so a locked user authenticates again as soon as its pods hold the new password.

### Upgrading to the next release: the other 25 service users rotate

> **From 0.3.67.** 0.3.65 shipped this and is broken (watcher's decision engine, ceilometer's upgrade), and so is
> 0.3.66 (barbican's db-sync); see the warnings in the install block. Upgrade from 0.3.64 straight to 0.3.67.

The release after 0.3.64 generates the remaining service users' passwords the same way, one
`openstack-credential-identity-user-<username>` each:

> aodh, barbican, ceilometer, cinder_swift, cloudkitty, cyborg, designate, freezer, glance, gnocchi, heat,
> heat-domain, heat-trust, ironic, magnum, magnum-domain, manila, masakari, mistral, octavia, skyline,
> swift, tacker, trove, watcher

After it, no service user keeps OpenStack-Helm's published password.

How each reader gets its password:
- **Main oslo.config file.** Each chart gets one drop-in, `<chart>-identity`
  (`/etc/<project>/<project>.conf.d/85-identity.conf`). It holds every rotated user's option from that
  chart's main config: `[keystone_authtoken] password`, `[service_credentials]`, `[nova]`, `[glance]`
  and the like. It also covers the options with other names:
  - heat's `stack_domain_admin_password` and `[trustee]`;
  - magnum's `trustee_domain_admin_password`;
  - cloudkitty's and masakari's `os_privileged_user_password`;
  - cinder's `backup_swift_key`.

  masakari's monitors read the same file from `/etc/masakarimonitors/masakarimonitors.conf.d/`.
- **Files that are not oslo.config drop-ins.** Two files are rendered whole by an ExternalSecret and
  mounted at the same path:
  - glance's `swift-store.conf`, from `glance-swift-store`;
  - skyline's `skyline.yaml`, from `skyline-config`.
- **swift's proxy** loads `proxy-server.conf.d/` once any drop-in exists, the memcache key or this one.
- **heat's and magnum's stack users** (domain-scoped) get an ExternalSecret openrc. Their domain ks-user
  Jobs rotate pre-upgrade with the lockout exemption, like the other ks-user Jobs.

The upgrade restarts every reader of a rotated user once.


Three checks keep this true:
- `hack/identity-users.yaml` records which chart reads which username. The credentials gate
  re-renders every chart and fails if that map is stale;
- check 17 fails unless every rotated user's ks-user Job is pre-upgrade and reads only its root;
- check 18 fails unless every reader's pod template changes when that one user's reference is
  applied.

### Upgrading to 0.3.67 or later: zaqar's signed-URL key and manila's service-instance key

From 0.3.67 two more secrets are generated per install:
- `openstack-credential-zaqar-signed-url-secret-key`. zaqar signs pre-signed queue URLs with it
  (`[signed_url] secret_key`). The chart published `SOMELONGSECRETKEY`. zaqar reads the generated key
  from a drop-in, `82-signed-url.conf`, in `/etc/zaqar/zaqar.conf.d/`. URLs signed before the upgrade
  stop verifying.
- `openstack-credential-manila-service-instance-ssh-key`, an SSH key pair from ESO's `SSHKey`
  generator. manila's generic driver now logs in to its service VMs (the share servers) with this
  key. It no longer uses `service_instance_password` (published: `manila`). It is not a generated
  password because the stock manila-service-image has user `manila` with password `manila` built in,
  so a generated password would lock manila out. manila-share mounts the key pair at
  `/etc/manila/service-instance-ssh/`. `[generic]` points `path_to_private_key` and
  `path_to_public_key` there. manila imports the public key as the Nova keypair `manila-service`, and
  each new service VM gets it through cloud-init. The image's password login is no longer used.

  **Replace share servers created before the upgrade.** They were booted without this key, so manila
  can no longer log in to manage them. They also still accept the published `manila`/`manila` login
  from anything that reaches the service network, which no setting on manila's side can change.
  Migrate their shares to new share servers, which boot with the key (`openstack share migration
  start`, or recreate the shares), then delete the old servers. An install with no generic-driver
  shares has nothing to do.

The ESO generators now include `SSHKey`, so the orchestrator also requires the
`sshkeys.generators.external-secrets.io` CRD. ESO 2.14.0, the version installed above, ships it.

blazar's physical-host plugin calls Nova as the `blazar` user. Up to 0.3.66 it still sent
OpenStack-Helm's published password (`[physical_host_plugin] blazar_password`), even though that user
has been generated since 0.3.63. From 0.3.67 it reads the generated password from
`91-physical-host-password.conf`.

### Upgrading to 0.3.68 or later: each service's MariaDB password rotates

From 0.3.68 every service's own MariaDB user gets a generated password:
`openstack-credential-oslo-db-<user>`, one per MariaDB username, for 28 users (aodh, barbican, blazar,
cinder, cloudkitty, cyborg, designate, freezer, glance, gnocchi, heat, horizon, ironic, keystone, magnum,
manila, masakari, mistral, neutron, nova, octavia, placement, powerdns, skyline, tacker, trove, watcher,
zaqar). Each user's password was OpenStack-Helm's published default (`password`), so any pod that reached
MariaDB could log in as any service and read or change its database.

One username is one MariaDB account (`'<user>'@'%'`), so it gets one root. nova's three databases
(`[database]`, `[api_database]`, `[cell0_database]`) and octavia's taskflow persistence database log in as the
same account, so the umbrella passes the same root at each of those keys (`extraPaths`). A chart refuses two
different references for one username.

On the upgrade that ships it:
- no service's rendered config carries a database URL any more. The URLs (`[database] connection` and its
  siblings, gnocchi's `[indexer] url`, designate's `[storage:sqlalchemy] connection`, masakari's
  `[taskflow] connection`, octavia's `[task_flow] persistence_connection`, zaqar's management store `uri`)
  arrive as a drop-in, `87-db-connection.conf` in the config's `.conf.d`, from an ExternalSecret
  `<service>-db-connection`. The password in it is URL-quoted. It reaches only the programs that use the database:
  nova's computes read `nova-compute.conf`, which has no database, and neutron's agents (OVS, DHCP, L3, metadata,
  OVN and the others) reach neutron-server over RPC, so they no longer hold the password at all. In neutron only
  neutron-server, the RPC server, db-sync, db-init, db-drop and the OVN database sync receive it
  (`hack/db-readers.yaml` records the measurement);
- `<service>-db-user` and nova's and octavia's sibling Secrets (`DB_CONNECTION`) become ExternalSecrets
  reading the same root;
- nova's cell mappings (`nova_api.cell_mappings`) no longer store a password. db-sync writes each cell's database
  and transport URL as a template, `{scheme}://{username}:{password}@{hostname}:{port}/{path}` (cell0: the same
  server and user, database `nova_cell0`), which every nova process formats from its own `[database] connection`
  and `[DEFAULT] transport_url`. With literal URLs, a nova pod that started before db-sync rewrote them kept the old
  password until it exited: nova caches each cell's connections per process. Existing mappings are rewritten on the
  upgrade;
- Horizon reads its database password from a file, `/etc/openstack-dashboard/krateo-db-password/password`,
  the way it reads `SECRET_KEY`. skyline's `skyline.yaml` is rendered by `skyline-config` with both its
  Keystone and its MariaDB password from their roots. powerdns reads its password from `powerdns-db-user`
  only;
- each chart's db-init now runs `ALTER USER '<user>'@'%' IDENTIFIED BY ...` after `CREATE USER IF NOT EXISTS`.
  Without it the generated password never reached MariaDB: `CREATE USER IF NOT EXISTS` leaves an existing
  user as it was. db-init and db-drop also read the config's `.conf.d`, where the URL now is;
- a literal URL in values (`conf.<service>.database.connection` and the other options above, skyline's
  `conf.skyline.default.database_url`, powerdns's `conf.pdns.gmysql-password`, a Horizon `local_settings`
  template without the stock `'PASSWORD'` line) is refused together with the reference: it would carry a
  password, and it would be replaced silently.

The rotation has a short window. MariaDB has no dual password, and db-init is a post-upgrade hook, so a pod
started with the new password can be refused until its chart's db-init has run `ALTER USER`, and a pod still
running with the old one is refused from then until it is replaced. That window is bounded by the rollout: no
reader keeps a password anywhere else (nova's cell mappings are templates of each pod's own config), so once every
pod runs the new configuration, nothing logs in with the old password. It happens on the upgrade that moves a user
to its root, and not again: the generated value is stable afterwards. MariaDB has no failed-login lockout (an access
denied does not count toward `max_connect_errors`), so no account is locked by it. Anything outside the
blueprint that logs in as a service's MariaDB user with the old password is refused from then on. It must
read the new value from the matching root Secret. Never delete those Secrets.

### Upgrading to 0.3.69 or later: each service's RabbitMQ password rotates

From 0.3.69 every service's own RabbitMQ user gets a generated password:
`openstack-credential-oslo-messaging-<user>`, one per RabbitMQ username, for 22 users (aodh, barbican, blazar,
ceilometer, cinder, cloudkitty, cyborg, designate, glance, heat, ironic, keystone, magnum, manila, masakari, mistral,
neutron, nova, octavia, tacker, trove, watcher). Each was OpenStack-Helm's published default (`password`), so any pod
that reached RabbitMQ could log in to a service's vhost and read or inject its RPC messages. This is the last tier of
#228. Each username is rendered by its own chart only (measured, `hack/mq-users.yaml`): the notification consumers
read other services' vhosts as the RabbitMQ administrator, not as their users, so each root reaches one chart.

On the upgrade that ships it:
- no service's rendered config carries a RabbitMQ URL any more. `[DEFAULT] transport_url` (and
  `[oslo_messaging_notifications] transport_url` in blazar, ceilometer and tacker) arrive as a drop-in,
  `88-transport.conf` in the config's `.conf.d`, from an ExternalSecret `<service>-transport`, with every host's
  password URL-quoted. It reaches only the programs that send or receive over RabbitMQ, measured per program from
  their source (`hack/mq-readers.yaml`): the services and RPC agents do; the database Jobs, the bootstrap Jobs, the
  cleanup CronJobs, neutron's netns-cleanup and OVN agents and octavia's health-manager, housekeeping and driver-agent
  never connect and no longer hold the password. nova's Jobs keep it: nova-manage builds a transport, and the cell
  mapping templates are formatted from it;
- neutron's agent health probes now also read `neutron.conf.d` (`--config-dir`). They parse the config without a
  project, so without it they would find no transport URL and report healthy without checking the agent's queue;
- `<service>-rabbitmq-user` (`RABBITMQ_CONNECTION`, read by rabbit-init) becomes an ExternalSecret reading the same
  root;
- rabbit-init's `rabbitmqadmin declare user` is an HTTP PUT of the user, which creates it or replaces it, so it sets
  the generated password on the existing user. Measured on the chart's `rabbitmq:3.13-management` image: run twice
  with two passwords, the second is accepted and the first refused. No script change was needed;
- a literal `conf.<service>.DEFAULT.transport_url` (or the notifications one) in values is refused together with the
  reference: it would carry the password, and it would be replaced silently.

glance and keystone also get a root, although neither runs rabbit-init (`manifests.job_rabbit_init: false`) and both
ship the `noop` notification driver: their configs carried the published password in a URL for a user that does not
exist, and a driver set through componentValues would use it.

The rotation window has the same shape as 0.3.68's MariaDB one. rabbit-init is a post-upgrade hook, so a pod started
with the new password is refused until its chart's rabbit-init has run, and retries. RabbitMQ authenticates only when
a connection opens, so a pod still running with the old password keeps its open connections; a reconnect with the old
password is refused until the pod is replaced. RabbitMQ has no failed-login lockout. Anything outside the blueprint that
logs in as a service's RabbitMQ user must read the new value from the matching root Secret. Never delete those Secrets.

ceilometer's notification agent now consumes only the vhosts that exist (#285). It had a fixed list of seven
(ceilometer, cinder, glance, nova, keystone, neutron, heat), and oslo.messaging retries a missing vhost forever: on a
full-strict install, where cinder is not selected and glance and keystone create no vhost, it consumed no
notifications at all. The umbrella now passes ceilometer `conf.notification_vhosts`: the vhosts of that list that a
selected component's rabbit-init creates (full-strict: ceilometer, nova, neutron, heat; full: cinder too). It follows
each component's own `componentValues`: `manifests.job_rabbit_init: false` drops that component's vhost, `true` adds
glance's (keystone's needs `manifests.secret_rabbitmq: true` as well: its chart ships the Secret its rabbit-init
reads switched off), and an `endpoints.oslo_messaging.path` override is consumed under its new name. An explicit
`componentValues.ceilometer.conf.notification_vhosts` still wins.

### Upgrading to 0.3.71 or later: bootstrap uploads each image once, and new image stores get 10 GiB

Measured on GKE (0.3.68 → 0.3.70, #295): manila's bootstrap ran while glance-api was rolling. Its `image show || create`
read "glance unreachable" as "image missing" and uploaded a second 952 MB `manila-service-image`. glance accepts two
images with one name. The 2 GiB `glance-images` store filled, glance-api went unready (its free-space floor, #163), and
glance, manila and nova never became Ready.

**Bootstrap.**
- glance's, manila's and ironic's bootstrap now count the images of each name with
  `openstack image list --name <name>`. If glance doesn't answer, the script fails and the Job retries; a failed lookup is
  never read as "absent". An image is uploaded only when glance answered with none.
- With two or more already there, nothing is uploaded and the Job logs it. Delete the extras yourself.
- ironic's cleaning-network Job does the same for its neutron network, which neutron also accepts twice under one name.
- `hack/validate-bootstrap-idempotent.py` fails any rendered script whose failed lookup leads to creating a resource
  whose API accepts a duplicate name. It also runs each image bootstrap against a stand-in `openstack`: glance
  unreachable, no image, one image, two images.

**The store.**
- A new install's `glance-images` claim requests 10Gi (`volume.defaultSize`); it was 2Gi.
- An existing claim keeps its size on every upgrade. The chart owns the claim (helm renders it, and `keep_pvc` keeps it
  on uninstall). It reads the live claim and renders that claim's own request, so no upgrade patches a claim you didn't
  ask to change. A StorageClass that can't expand, such as kind's local-path, therefore can't make an upgrade fail.
  An install created at 2Gi stays at 2Gi until you grow it.

**To grow an existing store**, first check that its StorageClass allows expansion:

```sh
kubectl -n openstack get pvc glance-images -o jsonpath='{.spec.storageClassName}{"\n"}'
kubectl get storageclass <that class> -o jsonpath='{.allowVolumeExpansion}{"\n"}'   # must print true
```

Then set `componentValues.glance.volume.size`, for example `10Gi`.
- The claim is raised to that size, never below its current request, because a claim cannot shrink.
- The value is the user's alone (empty by default), so the chart can tell a request from a default.
- If the class doesn't allow expansion, the API server refuses the patch ("only dynamically provisioned pvc can be
  resized and the storageclass that provisions the pvc must support resize") and glance's upgrade fails. Unset the value
  to recover.
- kind's default class, `standard` (`rancher.io/local-path`), sets no `allowVolumeExpansion`. That was read from its
  manifest in `kindest/node:v1.36.4`. local-path doesn't enforce the size anyway.
- The chart can't read the StorageClass itself: StorageClasses are cluster-scoped, and a lookup the render identity is
  refused would fail every render.

`hack/validate-glance-store.py` renders each case against a stand-in live claim:

| Case | Claim requests |
|---|---|
| new | 10Gi |
| live 2Gi | 2Gi |
| live 2Gi, `size: 10Gi` | 10Gi |
| live 20Gi, `size: 10Gi` | 20Gi |
| a size that isn't a quantity | refused |

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

Helm never deletes hook resources when it uninstalls a release, and a completed hook Job can't
delete itself (`hook-succeeded`), because kubernetes-entrypoint waits on dependency Jobs by name
for as long as the component is installed. So each chart also renders `<component>-hook-cleanup`,
a `pre-delete` hook that runs last at uninstall and deletes that release's install/upgrade hook
Jobs and their pods (#206). It runs in an image the chart's own Jobs already use, with a Role
limited to listing and deleting Jobs in the namespace. It always exits 0, so a failed cleanup
leaves the Jobs behind rather than blocking the uninstall. Jobs left by uninstalls before this
version stay: `kubectl -n <ns> delete jobs -l release_group=<release>` removes them.
`hack/validate-hook-cleanup.py` checks every chart that renders a hook Job, and runs the cleanup
against a stand-in API.

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

**On a PINNED component, any `spec.componentValues.<name>` property this blueprint adds after
the component's origin version therefore does nothing.** It is not rejected and it raises no
event: it is dropped on the way in, and the only symptom is a setting that has no effect. A
component is pinned only while its migration is not approved (next section); with the default
`upgradePolicy` it follows the migration and sees every value. If a value you set appears to be
ignored, read the component's `upgradePolicy` and the annotation above.

To give a pinned component a newer value, either approve its migration (next section), or
recreate it: delete its Composition and let the orchestrator emit it fresh, which re-stamps the
origin at the current version. Expect that component's workloads to be recreated.

### The pin and instance migration: one writer per component

The pin and core-provider's **instance migration** both decide which version a component's
object is written through, so only one of them may act for a component. Migration rewrites every
instance through the *new* endpoint and retires the old controller; a pin still writing the
object at its origin would undo that on every reconcile.

The umbrella reads each component's `CompositionDefinition` and pins only while its migration
is **not** approved, using the same predicate core-provider's engine evaluates:

| `spec.upgradePolicy` | Migration | The umbrella emits the component at |
| -------------------- | --------- | ----------------------------------- |
| `Automatic`, or unset (the default) | approved | the current version once served; until then, the newest served |
| `Manual` with `krateo.io/upgrade-to-version: <current served version, e.g. v0-3-48>` | approved | the same |
| `Manual` without that annotation, or naming any other version | not approved | its origin version (pinned) |
| `Paused` | not approved | its origin version (pinned) |

Up to 0.3.48 the umbrella pinned regardless of the policy. That was meant to be harmless under
`Automatic` because the old version used to be pruned, but core-provider now keeps a version
a release revision still uses, so on an upgraded install every component stayed at its creation
version: values a component gained since were dropped on store, and the old version was never
retired (#233). Measured on GKE, 0.3.47 → 0.3.48. From this fix, a component whose migration is
approved follows it.

```sh
# inspect
kubectl -n openstack get compositiondefinitions \
  -o custom-columns='NAME:.metadata.name,VER:.spec.chart.version,POLICY:.spec.upgradePolicy'

# keep a component on its origin version (opt it out of migration)
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

### New values land one pass later, at the new version

On an upgrade, a component whose migration is approved gets its **new values only once its
`CompositionDefinition` serves the new version**: `status.apiVersion` is the new
`composition.krateo.io/<version>` and its `Ready` condition has status `True` (any reason,
`ScaledToZero` included; `ControllerActive` is not required). Until then the umbrella re-emits the
component **unchanged**: its live spec, at the version it was last written at (its
`krateo.io/composition-version` label, which core-provider stamps on every write). Neither the
spec nor the label moves, so the controller that owned it keeps it. If the label is missing or
names a version the CRD no longer serves, the umbrella re-emits at the version it resolved instead.

Before this, the render that moved a definition to the new chart also wrote the new values at the
OLD API version, because the new one was not served yet. The object kept its old
`krateo.io/composition-version` label, so the old controller ran the old chart with the new
values. Measured on GKE 2026-11-15 (0.3.64 → 0.3.65, core-provider 2.13.102; #228): 3 of 33
components did that, and the old chart's pre-upgrade Job rotated passwords early.

What you see, and what it costs:
- During an upgrade a component does not change on the first pass(es); its new values land in
  the pass after its definition reports the new version `Ready`.
- A `componentValues` change made while a definition is not `Ready` (including outside an
  upgrade, if its reconcile is failing) is applied once it is `Ready` again.
- While held, the umbrella re-emits the whole live spec, so its release records every field in
  it. A field added to a component Composition by hand (not through `componentValues`) is
  therefore removed by the next write of the new values, where before it would have been left
  alone. Set component values through `componentValues`.
- If a component's Composition is deleted at the instant its spec is read, there is no live spec
  to keep, and the umbrella emits the values it computed, as before.
- A component that does not exist yet, and a component whose migration is not approved (pinned,
  above), are emitted exactly as before.

The mechanism is `osh.holdValues` and `osh.holdVersion` in the same file, asserted by
`hack/validate-two-phase-values.py`.

```sh
kubectl -n openstack get compositiondefinitions \
  -o custom-columns='NAME:.metadata.name,API:.status.apiVersion,READY:.status.conditions[?(@.type=="Ready")].status'
```

### A component reads Ready=False/HooksMayNotHaveRun

core-provider (#563, fixed in 2.13.108 by #569) can leave a component's first install `deployed` with its post-install
hooks never created. That happens when every composition-dynamic-controller loses its lease mid-install, for instance
while GKE resizes the control plane (measured 2026-11-17: cyborg's pods sat in `Init` forever). A core-provider that
carries the fix marks such a release `krateo.io/hooks-unverified`, and the component reads
`Ready=False/HooksMayNotHaveRun`. Removing that annotation by hand only stops the report. It does not run the hooks.

Recover in this order:

**a) Re-run the hooks with `rerunHooks.<component>`.** Set it on the Openstack resource to the current time:

```yaml
rerunHooks:
  cyborg: "2026-11-18T10:00:00Z"
```

- The umbrella passes the token to that component alone, as its `krateo.rerunHooksToken`. Every component schema
  declares it, and no template renders it.
- The component's values change, so core-provider runs a real helm upgrade. Every hook runs again: each install hook
  is also an upgrade hook, which `hack/validate-rerun-hooks.py` enforces.
- core-provider then clears the annotation itself. No other component moves.
- To run them again later, set a new time.

A label or annotation on the Composition would do nothing, because core-provider upgrades only on a change of chart
version, of the spec's values, or after healing drift. A spec key the CRD doesn't declare does nothing either: it's pruned.

**b) Or run the hooks by hand.** Render the component's chart at the deployed version and values, apply its hook Jobs
(`helm template ... --show-only` the hook templates), and wait for each to complete. Check what they set up (users,
databases, endpoints), then remove the `krateo.io/hooks-unverified` annotation.

**c) Last resort: delete the component's Composition.** The umbrella re-creates it, and a fresh install runs every hook
(measured on GKE: Ready 4 minutes after the delete). It is destructive: it uninstalls everything the first install
created, so only do this for a component whose state you can lose.

### Start ordering follows what each program calls (0.3.72)

Measured on a fresh 0.3.71 install on GKE:
- ceilometer-notification started before nova's vhost existed, and its workers died with `NOT_ALLOWED` until it did
  (#298).
- manila's bootstrap failed 7 times until nova-api was up (#299).

From 0.3.72:
- **ceilometer-notification waits for the vhosts it consumes.** It waits for the rabbit-init Job of every vhost it
  consumes (`conf.notification_vhost_jobs`, passed by the umbrella from the same list as `conf.notification_vhosts`).
  The cost: on a fresh install it starts only after nova, neutron and heat have run their rabbit-init Jobs, so it is
  the last of them to come up.
- **Every Job that calls an OpenStack API waits for that API.** Its kubernetes-entrypoint waits for each service its
  script's `openstack` commands call (`hack/validate-job-service-dependencies.py`, from an explicit map of CLI noun to
  service type).
- **manila's bootstrap now waits for keystone, glance, nova and manila-api.** So the umbrella creates manila after
  glance and nova. glance's storage-init and octavia's bootstrap now wait for keystone.

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
orchestrator also carries a `serviceContract` v1 stanza. Usage and health are emitted by
[krateo-platformops/cmp-usage-collector](https://github.com/krateo-platformops/cmp-usage-collector), not by this chart (its emitter was removed in 0.3.68).
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
- [docs/kolla-ansible-audit-and-helm-plan.md](docs/kolla-ansible-audit-and-helm-plan.md) —
  the upstream audit grounding the distribution-layer design.
- [docs/medium-openstack-as-a-service.md](docs/medium-openstack-as-a-service.md) — the
  narrative write-up.
- [docs/llms.txt](docs/llms.txt) — the version-pinned LLM doc index.

The dashboard (Horizon) logs in as `admin` with the install's generated password (domain `Default`): `kubectl -n openstack get secret openstack-credential-identity-admin -o jsonpath='{.data.password}' | base64 -d`:

| Login | Identity → Projects (admin) |
| ----- | --------------------------- |
| ![Horizon login](docs/horizon-login.png) | ![Horizon dashboard](docs/horizon-dashboard.png) |

## Develop & release

`.github/workflows/release-tag.yaml` packages every `blueprints/*/chart` and pushes each to
GHCR on a semver tag (`git tag 0.1.0 && git push origin 0.1.0` →
`oci://ghcr.io/krateo-blueprints/charts/<component>:0.1.0`).

`.github/workflows/lint.yaml` runs on every PR: the `hack/validate-*.py` gates (each states the
defect it was written for), the umbrella `componentValues` schema-in-sync check
(`hack/gen-componentvalues-schema.py`), the service-contract SSO deep-link resolution, the
removed-values refusal, and the shared `lint-docs` documentation-standard job. See [docs/release.md](docs/release.md).

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
