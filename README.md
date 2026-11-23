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
  boots to `ACTIVE`. Not possible on kind/Apple-Silicon: use an amd64 cluster whose nodes load the OVS kernel
  module (Ubuntu GKE nodes are one example).
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
# What 0.3.74 has actually been run through (2026-11-23), stated so you know what is NOT covered. All on 3-node GKE
# clusters (n2-standard-16, zonal, node MTU 1460), profile: full-strict, which is 31 components from 0.3.74 (manila is
# left out, #426):
#   - 0.3.74's tag commit freshly installed with these exact steps, as its pre-release (the published charts are
#     byte-identical to the tag's tree), on core-provider 2.13.111, with network.underlayMTU 1460: all 31 components and
#     the umbrella Ready 35 min after the apply, with no intervention. GKE resized the control plane mid-install; eight
#     composition controllers lost their leases and restarted, and no first install was interrupted. 242 pods, none
#     Pending; one container restarted (mariadb-controller, once). Every neutron agent and nova-compute ran on all 3
#     nodes with no MTU refusal, and neutron-server read 1460 for global_physnet_mtu and path_mtu. Every internal
#     catalog endpoint names an existing Service. No Keystone, MariaDB or RabbitMQ login was refused during the install.
#     Three services answered no valid request, or not all of them (#462, #463, below), although all 31 components read
#     Ready;
#   - the released 0.3.74 freshly installed the same way on core-provider 2.13.128: all 31 components and the umbrella
#     Ready 31 min after the apply, with no intervention and no container restarted (241 pods). The same checks passed.
#     GKE did not resize the control plane;
#   - the same again, with two first installs interrupted on purpose: tacker's and cyborg's composition controllers were
#     force-deleted while an install hook ran. 2.13.128 reinstalled each once (core-provider#629), every install hook
#     completed, and the 31 components and the umbrella were Ready 38 min after the apply. The databases were kept
#     (cyborg's with its drop hook enabled; an ordinary delete does drop it). Tacker's 3 volume claims were replaced by
#     new ones. GKE resized the control plane twice during cyborg's install. A tacker that had been deployed once was
#     then killed mid-upgrade: not reinstalled, its claims and data kept, its hooks run again;
#   - a fresh 0.3.71 install upgraded straight to 0.3.74 as "Upgrading an existing install" below and "neutron's underlay
#     MTU" describe (componentValues set before the upgrade), instrumented, on core-provider 2.13.111 only: 31 components
#     Ready 17 min after the upgrade started, with no intervention. It ran at 5bc3e765; the tag differs from it by one
#     image digest in libvirt's values that the default render does not use. manila was removed, with its Keystone
#     service, hook Jobs and 11 credentials (#444); the other 395 credential projections were byte-identical. keystone-api
#     did not restart for the deregistration. MariaDB and RabbitMQ restarted once each (84 s and 38 s). No login was
#     refused after the upgrade. Keystone, MariaDB and RabbitMQ accepted the generated passwords (33, 24 and 16 users)
#     and rejected every published one, and no published default remained in the 580 Secrets and ConfigMaps scanned.
#     Keystone's token API failed 10 of 90 samples around the MariaDB restart (2 min), and the other APIs 0 to 2 of 80
#     during their rollouts (#286); cyborg-conductor crashed once while placement restarted, and recovered;
#   - NOT run at 0.3.74: an upgrade on core-provider 2.13.128, profile: identity, OIDC federation, the Ceph path (#308),
#     and the recovery of a component that reads HooksMayNotHaveRun (its steps a and c ran on healthy components).
# Earlier releases, the same way:
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
#   - READY IS NOT WORKING for three services (#459): trove answers 500 to an instance list (#462), and masakari and
#     cloudkitty fail every request that carries a valid token (#463). 18 other services answered 200 to a list request
#     with the admin token; heat's CloudFormation API was not tried with a signed request. Earlier READMEs also named
#     blazar, freezer and magnum: those errors were the probe's own malformed requests;
#   - with publicEndpoints off, no public catalog endpoint is served (#451): use the internal interface
#     (--os-interface internal);
#   - any change to MariaDB's or RabbitMQ's credential ExternalSecret manifests restarts them, data or not (#449);
#   - if the API server goes away while a component is in its FIRST install (e.g. a managed control
#     plane resizing), its install hooks may never run and its pods wait forever. core-provider up to 2.13.107 does not
#     say so: delete that component's Composition, and the umbrella re-creates it (core-provider#563). 2.13.108 to
#     2.13.127 mark it Ready=False/HooksMayNotHaveRun (seen on a fresh 0.3.74 pre-release, 2.13.111, #297): see
#     "A component reads Ready=False/HooksMayNotHaveRun" below. 2.13.128 reinstalls it by itself (core-provider#629;
#     exercised above). The reinstall replaces the component's volume claims.
#   - cinder needs a block-storage backend: a Ceph cluster, through storage.backend ceph-rook or
#     ceph-external ("Storage: consuming a Ceph cluster" below; the Ceph path is not yet verified on a
#     live cluster, #308). With the default none, full-strict leaves it out, and selected anyway
#     (profile: full, or enabled:) its Composition reports "cinder needs a volume backend, and
#     storage.backend is none (#196)" instead of waiting in Init (see the full-strict section).
# NOT verified at this
# version: the 36-component profile: full, kind, a VNC console in a browser, and the
# per-component path further down.

BP_VERSION=0.3.74

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
#
# NODES WITH AN MTU BELOW 1500 (GKE's are 1460), with any profile that runs neutron: download the example, set
# `network.underlayMTU` in it to the nodes' MTU, and set OS to your copy. From 0.3.74 the OVS agent refuses to start
# without it (see "neutron's underlay MTU"). The published example is profile: identity, which runs no neutron.
kubectl create namespace openstack
OS=https://github.com/krateo-blueprints/krateo-openstack-blueprint/releases/download/${BP_VERSION}/example-openstack.yaml
i=0; until kubectl apply --dry-run=server -f "$OS" >/dev/null 2>&1; do
  i=$((i+1)); [ $i -lt 60 ] || { echo "the API server has not accepted $OS after 5 min:"; kubectl apply --dry-run=server -f "$OS"; break; }; sleep 5; done
kubectl apply -f "$OS"
```

`profile: identity` brings up MariaDB+Memcached+Keystone+Glance+Horizon; `profile: full`
brings up **all 36 components** (needs a large amd64 cluster — see `quickstart-gke.md`).

#### `profile: full-strict` — full, minus what needs provisioning you must do yourself

> **cinder now refuses to render without a block-storage backend (#196).** The Openstack
> Composition has a `storage.backend` (default `none`), and the umbrella passes it to cinder.
> With `none`, `full-strict` leaves cinder out, and a cinder selected anyway — `profile: full`, or
> `enabled:` — does not install: its Composition reports *cinder needs a volume backend, and
> storage.backend is none (#196)* instead of `cinder-volume` sitting in `Init` on `ceph-etc`.
> `full` itself is unchanged and still selects cinder, so a `full` install shows that one
> component failed rather than stuck. With a Ceph backend, `ceph-rook` or `ceph-external`, cinder
> installs (see "Storage: consuming a Ceph cluster"). Installs that provisioned Ceph by hand for the
> old `rbd1` precondition lose that path on purpose: it handed OpenStack the Ceph `client.admin`
> key, which #308 rules out. `componentValues.cinder.krateo.storageBackend` is refused:
> `storage.backend` is the one place that says which backend the install has. A standalone
> `Cinder` Composition must choose its own `krateo.storageBackend` (see "Upgrading: a standalone
> Cinder Composition must choose its storage backend").

> **manila now needs a share backend, and `full-strict` leaves it out (#426).** Upstream's
> default share backend, `generic`, never started here: its service-instance helper runs `ip`,
> which no airshipit manila image ships (measured on the images 0.3.23, 0.3.73 and 0.3.74 ran),
> and it needs Cinder volumes on Nova service instances. Until 0.3.74 nothing probed
> `manila-share`, so it reported `Ready` while its service thread had died at start; 0.3.74's
> readiness probe reports it truthfully, so an install that selects manila never reaches `Ready`.
> manila's umbrella entry now requires `shared-filesystem`, which nothing provides yet (CephFS
> comes with #308), so `full-strict` leaves it out. A manila selected anyway — `profile: full`,
> or `enabled:` — does not install: its Composition reports *manila needs a share backend, and
> none is configured (#426)*. To run manila with a backend of your own, set
> `componentValues.manila.krateo.shareBackend: unmanaged` and configure the backend under
> `componentValues.manila.conf.manila`; the generic driver is refused there too.

> **Upgrading an existing `full-strict` install to 0.3.74 or later uninstalls its `manila`
> Composition.** That manila never served a share (see the notice above).

> **Upgrading an existing `full-strict` install to 0.3.43 or later uninstalls its `cinder`
> Composition.** From 0.3.43 `cinder` is one of the components `full-strict` leaves out, because
> its only shipped volume backend is Ceph and nothing provides a Ceph cluster (#196). On a
> default install that cinder never worked: `cinder-volume` never started, and from 0.3.41 its
> `storage-init` hook held the release in `pending-install`. Measured on #179 and on a cold
> install (#204). Up to the #196 fix, an install that had provisioned Ceph could keep cinder with
> `profile: full` or `enabled:`; that path is now closed (see the notice above).

Five components — `cinder`, `ironic`, `manila`, `octavia` and `swift` — each require infrastructure
that no Kubernetes cluster provides and that this blueprint does not create, listed as a capability
under `components[].requires` in the umbrella (`block-storage`, `pxe-interface`,
`shared-filesystem`, `lb-mgmt-net`, `rwx-storage-class`). They are in `profile: full`, and they
stay there. `profile: full-strict` is the same set **minus each one whose capability the install
does not provide**. Only `block-storage` can be provided today, by `storage.backend`; with the
default `none` that is all five:

```yaml
spec:
  profile: full-strict   # 31 components, none of which needs provisioning you must do yourself
```

It also needs one node label fewer — three rather than four, since octavia is the only consumer
of `openstack-network-node` (see the labelling block above).

| component | needs | symptom when it is absent |
|---|---|---|
| `cinder` | `block-storage`: a `storage.backend` other than `none` — a Ceph cluster, consumed as "Storage: consuming a Ceph cluster" describes | its Composition reports the render error *cinder needs a volume backend, and storage.backend is none (#196)*. Before the fix: `cinder-storage-init` and `cinder-volume` stayed in `Init` on `FailedMount` for `ceph-etc` |
| `ironic` | an interface named `ironic-pxe` on each conductor node — the bare-metal provisioning NIC | `ironic-conductor-init` exits 1 with `Could not find IP for pxe to bind to`; the pod sits in `Init:CrashLoopBackOff` |
| `manila` | `shared-filesystem`: a share backend that runs on the airshipit image. None is provided yet (CephFS comes with #308); `componentValues.manila.krateo.shareBackend: unmanaged` with your own backend under `conf.manila` | its Composition reports the render error *manila needs a share backend, and none is configured (#426)*. Before the fix: `manila-share` stayed `Running` with its service thread dead on `FileNotFoundError: 'ip'`, and from 0.3.74 its probe reported *no ESTABLISHED connection to RabbitMQ* |
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

`hack/provisioning-preconditions.yaml` carries each one with its capability and a command you can
run to check it, and `hack/validate-provisioning-preconditions.py` asserts **both** halves by
rendering the chart: every such component is still selected by `full`, none is selected by
`full-strict` with `storage.backend: none`, and each declares what it needs, its symptom and that
check. As a positive control it renders `full-strict` with every other backend too, which must add
cinder and nothing else. `hack/validate-storage-backend.py` asserts the value reaches cinder alone
and that cinder refuses without a backend. Neither profile can drift from the other
silently, and a precondition cannot describe a component that no profile distinguishes.

To take just one of them on top of `full-strict` — say you have the RWX StorageClass but no PXE
NIC — name it in `enabled:`, which overrides `profile` and resolves its dependency closure:

```yaml
enabled:
  - swift
```

#### Monitoring: recommended when alert-provider is installed

Add `monitoring.enabled: true` to the `Openstack` spec above. It is off unless you ask, and the umbrella refuses it on
a cluster without alert-provider's CRDs. See "Monitoring" below.

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

## Storage: consuming a Ceph cluster

cinder's volumes — and, if you choose, glance's images — live in Ceph (#308). The blueprint consumes **any** Ceph
the same way and never holds the Ceph `client.admin` key: the cluster's side creates the users, their caps and the
pools, and mints the keys; the umbrella **copies** each key and the monitor addresses into its namespace with
External Secrets Operator, and the charts project the copies. Nothing here depends on a cloud provider.

```yaml
spec:
  profile: full-strict          # now includes cinder
  storage:
    backend: ceph-rook          # or ceph-external
    ceph:
      sourceNamespace: rook-ceph
      glanceStore: rbd          # optional: images in Ceph too (default pvc)
```

**What must exist on Ceph's side** (with Rook: a `CephBlockPool` per pool and a `CephClient` per user; otherwise the
Ceph admin), with the names in `storage.ceph` (defaults shown):

- pools `cinder.volumes` (and `glance.images` for `glanceStore: rbd`);
- `client.cinder` with `mon 'profile rbd'`, `osd 'profile rbd pool=cinder.volumes, profile rbd-read-only pool=glance.images'`;
- `client.glance` with `mon 'profile rbd'`, `osd 'profile rbd pool=glance.images'` (only for `glanceStore: rbd`).

These are the caps the Ceph documentation gives for OpenStack (docs.ceph.com, "Block Devices and OpenStack"). The
blueprint checks, with each user's own key, that its pool exists and the user can use it: `cinder-storage-init`
fails with `client.cinder cannot list pool cinder.volumes` when either is missing.

**Where the keys come from:**

- `ceph-rook`: Rook keeps a CephClient's key in Secret `rook-ceph-client-<name>` (key `userKey`) and the monitors in
  `rook-ceph-config` (key `mon_host`), in the CephCluster's namespace (`storage.ceph.rook` if yours differ).
- `ceph-external`: the Ceph admin places each user's key and `mon_host` in Secrets and names them under
  `storage.ceph.keys.{cinder,glance,monHost}` (each `secret` and `key`); the umbrella refuses an empty one.

**The read grant.** The copies are read by the ServiceAccount `openstack-credentials-ceph-reader` in the Openstack
namespace. If the source Secrets are in that namespace, the umbrella grants it itself. If they are in another (Rook's),
that namespace's owner grants exactly those Secrets — for example:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: openstack-ceph-reader
  namespace: rook-ceph
rules:
  - apiGroups:
      - ""
    resources:
      - secrets
    resourceNames:
      - rook-ceph-client-cinder
      - rook-ceph-client-glance
      - rook-ceph-config
    verbs:
      - get
  - apiGroups:
      - authorization.k8s.io
    resources:
      - selfsubjectrulesreviews
    verbs:
      - create
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: openstack-ceph-reader
  namespace: rook-ceph
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: openstack-ceph-reader
subjects:
  - kind: ServiceAccount
    name: openstack-credentials-ceph-reader
    namespace: openstack
```

Without it, External Secrets Operator reports the copy store `openstack-credentials-ceph` not Ready, and the Openstack
Composition **refuses to render**, naming the namespace, the ServiceAccount and the exact Secrets to grant. A Ceph
consumer (cinder, libvirt, glance with `rbd`) is **created only once the copies of the keys it reads report Ready**:
until then it is listed in `openstack-install-pending`, like a dependency that is not Ready yet, and never starts on
a key nothing has copied. Check with
`kubectl get secretstore openstack-credentials-ceph` and
`kubectl get externalsecret openstack-credential-ceph-client-cinder openstack-credential-ceph-mon-host`.

**Rotation.** A key Ceph's side rotates (Rook's cephx rotation, or the Ceph admin) reaches the copies within 5 minutes,
then the projections. Each reader — `cinder-volume`, `glance-api`, `libvirt` — carries a checksum of the key it
mounts, so the next reconcile restarts it. A cephx user holds one key at a time, so a rotation interrupts that user
until its readers restart; libvirt defines its secret at start. What a running VM's open connection to an attached
volume does across a rotation is not measured yet (#308, live test L4): rotate in a maintenance window.

**Not covered yet:** nova's ephemeral disks stay on the hypervisor (Ceph-backed ephemeral disks are a later phase of
#308); and this path has been rendered and gated, not yet run on a live cluster.

**Moving an existing glance to `rbd`.** glance stops mounting its `glance-images` claim, so every image stored there
would become unreadable. While the claim exists, glance refuses to render until you set
`storage.ceph.glanceImagesMigrated: true`: first copy each image into the rbd store (with both stores enabled,
`glance image-import <id> --import-method copy-image --stores rbd`), or confirm there is nothing to keep. The claim
itself is kept (`keep_pvc`) until you delete it.

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

A cluster's default class is usually block storage, which **cannot do RWX at all**. For example, GKE's default
`standard-rwo` can't, and its RWX-capable classes are Filestore-backed and not the default. Other platforms differ in
names, not in this.

So **before selecting swift, make sure the cluster has an RWX StorageClass, and name it**:

```yaml
spec:
  componentValues:
    swift:
      ring:
        shared_storage:
          storageClassName: <your-rwx-class>    # whatever your platform calls it
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

## Cluster size

From 0.3.75, every container requests CPU and memory (#283), and every API runs two replicas (#286).
Until then, every pod was BestEffort and requested nothing, so no size could be read from the charts.

**What the defaults request**, summed from the rendered charts (control plane = Deployments and StatefulSets, replicas
included; DaemonSets count once per node they run on):

| profile | control-plane pods | control-plane requests | DaemonSets, per node |
|---|---|---|---|
| `identity` (5 components) | 8 | 0.9 CPU, 2.2 GiB | none |
| `full-strict` (32) | 92 | 9.8 CPU, 14.6 GiB | 1.3 CPU, 1.9 GiB |
| `full` (36) | 106 | 11.4 CPU, 17.2 GiB | 1.8 CPU, 2.9 GiB |

So `full-strict` on three nodes requests about 14 CPU and 20 GiB, before the cluster's own system pods. During a
rollout each rolling Deployment adds one surge pod, and each hook Job requests 0.1 CPU and 128 MiB while it runs.

**The minimum cluster:**
- `identity`: one node with 2 CPU and 4 GiB allocatable. One node gives no availability: both replicas of each API
  share it.
- `full-strict`: 16 CPU and 24 GiB allocatable in all, over at least 2 nodes, so that an API survives the loss of
  one. Requests are not the whole story: an earlier measurement, before any requests existed, saw about 10 CPU and
  35 GiB in use across all 32 components during an install or upgrade, and 4.5 CPU at rest. The verified installs
  above ran on 3 nodes with 16 vCPU and 64 GB each, which leaves ample room. These requests have not been installed on
  a cluster yet.

**The figures are OpenStack-Helm's own per-container ones** (mostly 100m CPU and 128 MiB requested, with a 1 GiB memory
limit). They are not measured yet; per-container measurement is #283's open half. Larger limits, each with its reason in
the chart's values.yaml, apply to the containers that need more memory:
- mariadb, rabbitmq, memcached and redis;
- APIs that fork several workers (horizon, neutron-server and neutron-rpc-server, mistral, trove, tacker, skyline,
  octavia);
- glance-api, cinder-volume, nova-compute and ovs-vswitchd.

**No CPU limits** by default. A CPU limit throttles an API worker even on an idle node, which shows up as latency and
probe timeouts rather than as a clear failure.

**Change any of it** through `componentValues.<component>`, with OpenStack-Helm's own keys:

```yaml
componentValues:
  keystone:
    pod:
      replicas:
        api: 3
      resources:
        api:
          requests:
            cpu: 500m
            memory: 512Mi
          limits:
            memory: 2Gi
```

`pod.resources.enabled: false` turns sizing off for a component. Fewer replicas also work: `pod.replicas.api: 1` brings
back a drop during each rollout, and the PodDisruptionBudget then allows the one pod to be evicted rather than block a
node drain.

**How each API stays up:**
- **Two replicas.** Every Deployment that a Service selects runs at least two replicas and rolls with
  `max_unavailable: 0` and `max_surge: 1`: a new pod is Ready before an old one goes.
- **PodDisruptionBudgets.** Each multi-replica workload has a PodDisruptionBudget with `min_available: 1`.
- **Spread.** Each prefers a different node, and a different zone when the nodes carry `topology.kubernetes.io/zone`.

The exceptions are named, never silent:
- **glance-api and tacker run one pod each.** They mount ReadWriteOnce claims, so a second pod, or a surge pod, on
  another node would wait on Multi-Attach forever. glance keeps a rolling update with `max_unavailable: 1`, and tacker
  uses Recreate. A shared store lifts this (#308).
- **redis runs one pod.** It is an unreplicated in-memory store, and a second replica would be a second, empty store
  (`hack/single-replica-serving.yaml`, #309).
- **The two metrics exporters run one pod each** (`openstack-exporter`, `openstack-mysqld-exporter`, with `monitoring` on).
  They are scrape targets, not APIs: a second replica would poll every source twice and publish every series twice. A
  rollout costs a short scrape gap (same file, #312).

`hack/validate-api-availability.py` and `hack/validate-pod-resources.py` assert all of this against the rendered
charts.

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

## Public endpoints: the APIs and Horizon over HTTPS (#311)

Off by default. Without it, OpenStack is reachable from inside the cluster, plus Horizon on NodePort 31000 over plain
HTTP; the public catalog URLs point at Services nothing serves. With `publicEndpoints.enabled`, the orchestrator renders
one Gateway API `Gateway` and an `HTTPRoute` per public endpoint of every selected component, and moves each public
catalog URL to `https://<service>.<baseDomain>`:

```yaml
spec:
  publicEndpoints:
    enabled: true
    baseDomain: cloud.example.com          # keystone.cloud.example.com, nova.cloud.example.com, ...
    tls:
      issuerRef:
        name: krateo-selfsigned-ca         # any cert-manager ClusterIssuer (or kind: Issuer)
    networkPolicy:
      nodeCIDRs:
        - 192.168.10.0/24                  # your nodes' address range (example)
      allowNamespaces:
        - krateo-system                    # e.g. where the OpenStack KOG operators run
```

**Prerequisites:** a Gateway API implementation (the CRDs plus a controller for `gatewayClassName`, default
`agentgateway`) and cert-manager. Krateo's installer brings both with `features.ingress`. On a live cluster the
orchestrator refuses to render, with the reason on its status, while the Gateway API or cert-manager CRDs are absent.
Then point DNS for the hostnames, or a wildcard `*.<baseDomain>`, at the Gateway's address
(`kubectl -n openstack get gateway openstack-gateway`). Krateo's `features.externalDns` does that from the HTTPRoutes.
Set `gateway.addresses` to a reserved IP so a rebuild keeps the records valid.

Nothing here is specific to a cloud. The class, the issuer, the domain and the node CIDRs are all inputs, and the
chart renders no load-balancer annotations and assumes no DNS provider. How the Gateway gets an external address
(a cloud load balancer, MetalLB, or something else) is up to the Gateway API implementation.

**What it renders and changes:**

- **The Gateway** `openstack-gateway`: an HTTPS listener on 443 with the Certificate `openstack-gateway-tls`, and an
  HTTP listener on 80 that only redirects to HTTPS.
- **The Certificate:** issued by `tls.issuerRef` for every routed hostname. A private CA (such as Krateo's
  `krateo-selfsigned-ca`) needs clients to trust it: `export OS_CACERT=ca.crt`, or `openstack --os-cacert ca.crt`. An
  ACME issuer must be able to validate every hostname. DNS-01 works; an HTTP-01 solver attached to a different Gateway
  cannot.
- **One HTTPRoute per public endpoint:** every catalog service a selected chart registers, plus Horizon and the noVNC
  console proxy. Each route sends to that component's ClusterIP Service. Every response carries
  `Strict-Transport-Security: max-age=31536000`. Every request gets `X-Forwarded-Proto: https`, overwriting what the
  client sent, so the services' proxy-header parsing cannot be lied to.
- **Never routed:** nova-metadata, RabbitMQ (and its management UI), MariaDB, memcached.
- **Each component's public endpoint** (`endpoints.<type>.host_fqdn_override.public`, scheme `https`, port 443) moves to
  its hostname. Its `[oslo_middleware] enable_proxy_headers_parsing` is set, so version-discovery links say `https`.
  Its own Ingress objects are switched off: they were inert, because no controller serves their class. nova's noVNC
  base URL and glance's CORS origin follow too.
- **Horizon:** its Service becomes `ClusterIP`, so **NodePort 31000 goes away**. Its cookies become `Secure`, it
  trusts `X-Forwarded-Proto`, and it accepts CSRF only from `https://horizon.<baseDomain>`.
- **An ingress NetworkPolicy for the install namespace.** It denies everything by default, then allows:
  - this namespace;
  - `nodeCIDRs`. These are required: neutron's agents, nova-compute, libvirt and openvswitch run on the host network,
    so their traffic arrives from node addresses;
  - the Gateway's proxy pods, to the routed backends' ports;
  - the internet, to the proxy pods only;
  - `allowNamespaces`.

  Nothing else crosses into the namespace. That includes the OpenStack KOG operators, unless you list their
  namespace. Set `networkPolicy.enabled: false` to leave policy to yourself.

  The policies select every pod that lacks the label `krateo.io/networkpolicy-exempt`. A pod given that label is
  selected by none of them and is left unrestricted: an explicit, visible opt-out.

**Turning it on for an existing install** changes the values of every component it touches once, so each runs a helm
upgrade. The ks-endpoints hooks then re-register the public catalog URLs. Anyone using `:31000` must move to
`https://horizon.<baseDomain>`.

**Not verified on a cluster yet.** `hack/validate-public-endpoints.py` checks the rendered objects and every
component chart rendered with the values the orchestrator gives it. A release run still has to show:

1. From a machine outside the cluster, `openstack --os-cacert ca.crt --os-auth-url https://keystone.<baseDomain>/v3
   --os-interface public token issue` succeeds, and `catalog list` shows only `https://...<baseDomain>` public URLs.
2. One call per routed service also succeeds through its public URL.
3. `curl -sI https://horizon.<baseDomain>/` shows `Strict-Transport-Security`, `http://` answers 301, and a browser
   login works with `Secure` cookies.
4. The NetworkPolicy is enforced. A pod in another namespace connecting to `memcached:11211`, `mariadb:3306`,
   `rabbitmq:5672` or `keystone-api:5000` **must be refused**. If it connects, the cluster's network plugin is not
   enforcing NetworkPolicy, and the run fails rather than passes.
5. A server still boots and gets its address, which shows the node allowance kept the host-network agents working.
6. The Gateway answers a hostname it does not route, and an internal name such as `keystone-api.openstack.svc`,
   with 404.

Two consequences to know:

- **In-cluster clients that use the public interface now reach the external hostname, so they need cluster DNS to
  resolve it and must trust the CA.** heat's own heat client (`clients_heat`, publicURL, as upstream) and castellan's
  `barbican_endpoint_type` (upstream default `public`, used by nova and cinder for encrypted volumes) are two such
  clients. Before this they reached a Service with no pods.
- **Not routed in this release:** Skyline, and nova's spice and serial consoles.

## Editing or deleting a root Secret

Each generated credential's root is an External Secrets Operator `ExternalSecret` (`refreshPolicy: CreatedOnce`,
`creationPolicy: Owner`) writing the Secret `openstack-credential-<id>`. What touching that Secret does, from ESO
v2.14.0's source (`pkg/controllers/externalsecret/externalsecret_controller.go:370`, `isSecretValid`):

- **Editing its data regenerates it.** ESO skips a refresh only while the Secret's data still matches its data-hash
  annotation. An edit breaks that, and ESO writes a **fresh random value**, neither the one you wrote nor the old one.
  The services keep the value they were given until their own copies refresh, so an edit is an uncoordinated
  rotation, not a way to set a password.
- **Deleting it** does the same: ESO recreates it with a new value.
- **The blueprint detects both and reports them.** It latches a sha256 of each generated root (ConfigMap
  `openstack-credential-generations`). A root whose data changes is listed on the Openstack resource's
  `status.credentialDrift` by its Secret's name, and a ConfigMap `openstack-credential-drift-<Secret>` shows why and the
  root's current sha256. **No live credential follows it:** every projection of a generated root refreshes only when it
  itself changes (`refreshPolicy: OnChange`), so the services and their backends keep the previous value.
- **To accept the new value** (when you know the edit was deliberate and will set it in the backend yourself), set the
  sha256 the report shows. It re-baselines only if that is still the root's current data:

```yaml
credentialDrift:
  acknowledge:
    openstack-credential-oslo-db-keystone: "<sha256 from ConfigMap openstack-credential-drift-openstack-credential-oslo-db-keystone>"
```

### Which credentials rotate

Only the RabbitMQ service users, with `messaging.backend: operator` (#309): `rotate.oslo-messaging-<service>` for each
messaging component whose rabbit-init runs (the schema rejects any other id, and the umbrella refuses `rotate` with the
chart-mode broker, where nothing would rotate). Every other credential rotates only through a release that introduces a
new root, as the "Upgrading to …" sections below describe.

- **A rotation is a new root, never a changed one.** `rotate.oslo-messaging-heat: "<token>"` makes the umbrella mint
  `openstack-credential-oslo-messaging-heat-g<first 8 hex of sha256(token)>`, once, stamped with the root it replaces.
  Use the current time as the token.
- **Keep the token set to its last value afterwards.** Removing it is refused (it would re-activate the old root), and
  so is a token whose generation already existed. Rotate again with a new token.
- **The active generation** is the root the component's Composition references:
  `kubectl -n openstack get heat heat -o jsonpath='{.spec.endpoints.oslo_messaging.auth.heat.passwordFrom.secret}'`.

### Rotating a RabbitMQ service user

One step: set `rotate.oslo-messaging-<service>` to a new token (the current time) and apply. The umbrella does the rest
over the next reconciles, deriving each step from what exists in the cluster (#309 C2):

1. **Minting.** The new root, and the umbrella's projection of it,
   `<service>-rabbitmq-topology-user-g<8 hex>` (username `<service>-g<8 hex>`). Once that Secret exists, the service's
   release renders a second RabbitMQ user, `<service>-rabbitmq-user-g<8 hex>`, with its own Permission on the same vhost,
   so no queue moves. Both users work.
2. **Switch.** Once the new User is Ready, the service's Composition names the new root and username. Every pod of the
   service rolls: each pod template carries `krateo.io/messaging-generation: -g<8 hex>`, because nothing else in a pod
   template changes when only a Secret that External Secrets projects changes. Pods restarting meanwhile wait on the
   active generation's User.
3. **Draining.** The previous user stays until every reader has moved:
   - every Deployment, StatefulSet, DaemonSet and CronJob of the release carries the new generation, and has rolled out
     as `kubectl rollout status` judges it (kubectl v1.36.4). A StatefulSet partition counts as not rolled while any pod
     is below it;
   - no Job or CronJob Job is still running;
   - the service's release for the switched spec has been applied, **hooks included**: its Composition's
     `status.observedGeneration` has caught up. core-provider's controller writes it only after the helm upgrade,
     which runs and waits for the hooks, has succeeded. A Composition marked `krateo.io/hooks-unverified` waits until
     you have checked its hooks and removed the mark;
   - the service's projections of the new root (`<service>-rabbitmq-user`, `<service>-transport`) have synced, so every
     pod started from then on reads the new credential.
4. **Retired.** The previous user leaves the release. Its User is deleted, and the topology operator deletes the
   RabbitMQ user.

While a rotation is in progress, `status.credentialRotation` lists its id. ConfigMap
`openstack-credential-rotation-<id>` shows the phase (`minting` or `draining`) and what it waits on:

```
kubectl -n openstack get configmap openstack-credential-rotation-oslo-messaging-heat -o jsonpath='{.data.blockers}'
```

**Some waits need you; nothing is forced.**
- A StatefulSet or DaemonSet with `updateStrategy: OnDelete`, or a bare Pod, never moves by itself. Delete its pods, one
  at a time.
- A partition above 0 keeps its pods on the previous user. Set the partition to 0.
- A workload the umbrella cannot read is kept as a blocker, never assumed rolled.
- A CronJob whose Jobs are always running holds the previous user for as long as they run.

A second token set while the previous rotation still drains waits for it: never more than two users at a time. Edit or
delete neither root during a rotation: an edit of a generation root is reported like any other
(`status.credentialDrift`).

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
read the new value from the matching root Secret. Never delete or edit those Secrets: either regenerates the value ("Editing or deleting a root Secret").

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
logs in as a service's RabbitMQ user must read the new value from the matching root Secret. Never delete or edit those Secrets: either regenerates the value ("Editing or deleting a root Secret").

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

### Upgrading to 0.3.74 or later: MariaDB and RabbitMQ restart once

The first upgrade to 0.3.74 restarts the MariaDB server and the RabbitMQ server once each, on every install. Each runs
one replica, so expect a short outage of what needs them. Measured on GKE upgrading 0.3.71 (r074): the RabbitMQ pod
was down for about 40 s (terminating to Ready, 18:33:24-18:34:01Z) and the MariaDB pod for about 70 s
(18:34:27-18:35:34Z); Keystone's token API failed for about 2 minutes around the MariaDB restart. The upgrade's own
Keystone-user Jobs retry until it is back. Nothing has to be done afterwards.

Why each pod template changes (a field-by-field render diff of 0.3.71 against this release):
- **MariaDB server:**
  - its dbadmin, sst and dbaudit credential ExternalSecrets changed from an hourly refresh to
    `refreshPolicy: OnChange` (#422), and the pod template carries a hash of each of those manifests;
  - `configmap-bin-hash` changed. It was the hash of the whole `mariadb-bin` ConfigMap, which also holds scripts the
    server never runs, and 0.3.74 changed one of them, the controller's `mariadb_controller.py` (#335). Since #445 the
    server hashes only the keys it mounts (`start.py`, `stop.sh`, `health.sh`), so later changes to the controller's,
    the test pod's or the Jobs' scripts no longer restart the database; the new value is itself part of this one
    restart. `hack/validate-mariadb-bin-hash.py` checks it.
- **RabbitMQ server:** the hashes of its admin and Erlang-cookie ExternalSecrets changed (#422, as above), and its
  `rabbitmq-password` init container's image moved to the dated tag `2026.1-ubuntu_noble-20261117` (#343).
- **Plan for it:** upgrade when a few minutes without the database and the message queue are acceptable.

### Upgrading: a component that leaves the selection is removed from Keystone

When a component leaves the selection, the umbrella removes what it registered:
- **When it applies:** an upgrade that drops it (0.3.74's full-strict drops manila), or your own change of `profile` or
  `enabled`.
- **Why it's needed:** `helm uninstall` of the removed component runs its OLD chart's hooks, which never did this. Its
  catalog entries outlived it and pointed at a deleted Service. On the 0.3.71 -> 0.3.74 upgrade, that kept Skyline,
  and with it the umbrella, from ever going Ready (#444).
- **How it works:** on every reconcile the umbrella looks up, by name, the component's Composition and every Service its
  chart renders.
  - While any of them exists, the component is still being removed. Nothing is deregistered, and the ConfigMap
    `openstack-deregistration-pending` names what is still there.
  - Once none exists, the component is handed to keystone as `krateo.deregister`. That changes keystone's values, so
    keystone is upgraded, and its hook Job `keystone-deregister` deletes the component's Keystone services, with their
    endpoints, and its leftover hook Jobs and their pods.
  - Nothing is remembered between reconciles, so the order in which the removal and keystone's upgrade happen doesn't
    matter, and deleting what is already gone is a no-op.
  - Selecting a component takes it back out of `krateo.deregister`. So any change of the selection, including
    `monitoring.enabled`, upgrades keystone once and runs its hooks again. Its pods don't restart: `krateo.deregister`
    reaches only the hook Job.
- **What is deleted:**
  - only a service whose type AND name are what the component registers. The list is derived from the charts' own
    ks-service Jobs;
  - never a type a selected component registers. A service you created by hand with the same type and another name is
    kept, and the Job's log names it;
  - only hook Jobs labelled with the component's `application` name.
- **A component you run on its own is left alone:** if you run, say, Manila's standalone Composition next to the
  umbrella with manila not selected, its `manila-api` Service exists, so it stays listed in
  `openstack-deregistration-pending` and its catalog entries are never deleted.
- **Nothing to do on upgrade:** the first upgrade to this release removes what earlier removals left.
- **A pinned keystone gets nothing:** a keystone Composition whose migration is not approved (`Paused`, or `Manual`
  without the annotation, above) is read through its origin schema. It never receives `krateo.deregister`, so nothing
  is deregistered. Approve its migration, or delete the service by hand: `openstack service delete <id>`.

**Skyline no longer depends on every catalog endpoint resolving.** Its nginx used to refuse to start when any catalog
host did not resolve. Now each endpoint location resolves per request through the cluster DNS, so a dead endpoint
answers that location with 502 and the dashboard still serves. Upstream divergence `skyline-444`;
`hack/validate-skyline-nginx.py` runs `nginx -t` on upstream's own template before and after.

### Upgrading: a standalone Cinder Composition must choose its storage backend

From #308 step 4, cinder's chart default for `krateo.storageBackend` is `none`, which refuses to render, instead of
`unmanaged`, upstream's `rbd1` wiring (#196).

- **Through the Openstack Composition: nothing to do.** The umbrella always passes `storage.backend` (`none` by
  default) and refuses a `componentValues` override, so cinder never renders at its chart default.
- **A standalone `Cinder` Composition** (created from the Cinder CompositionDefinition, without the umbrella) whose
  spec does not set `krateo.storageBackend` refuses at its next render after the upgrade, naming the key. Its render
  fails before any helm upgrade, so the release stays at its previous revision until the spec chooses. Set it
  **before** upgrading:
  - `krateo.storageBackend: unmanaged` keeps upstream's wiring exactly: you provide the ConfigMap `ceph-etc` and the
    Secrets `pvc-ceph-client-key` (which holds Ceph's `client.admin`) and `cinder-volume-rbd-keyring`;
  - or `krateo.storageBackend: ceph-external` with `krateo.ceph.keyringFrom` and `krateo.ceph.monHostFrom`, each a
    reference (`store`, `secret`, `key`) to a Secret holding the cinder user's key and the monitors' `mon_host`.

Before this change, such a Composition with no `krateo.storageBackend` rendered upstream's wiring silently, which works
only when those three objects exist; otherwise cinder-volume waited in `Init` forever.

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

### neutron finds its tunnel interface by itself

neutron's `network.interface.tunnel`, the host NIC that carries VXLAN tunnel traffic, defaults to empty. The OVS
agent's init then uses the interface of the default route, which works on any node. Set it only when tunnel traffic
must leave through another NIC.

**Existing installs keep `ens4`.** The field used to default to `ens4`, a GCE NIC name. That old schema default was
written into each stored neutron object (#179), and upgrading doesn't remove it.
- On GCE, `ens4` is the right NIC, so nothing changes.
- On any other node, the OVS agent's init stops with `Var LOCAL_IP is empty`. Clear the field, or set the node's NIC:

  ```yaml
  componentValues:
    neutron:
      network:
        interface:
          tunnel: ""        # or the NIC, e.g. eth0
  ```

### neutron's underlay MTU

neutron's VXLAN networks ride on the node NICs that carry the tunnel traffic, the underlay. Each VXLAN packet carries
50 bytes of overhead, so neutron gives a VXLAN network an MTU of the underlay's MTU minus 50, and tells its guests so
through DHCP. It learns the underlay's MTU from `network.underlayMTU`, default 1500, which reaches neutron as
`global_physnet_mtu` and ML2 `path_mtu`.

**Set it when your nodes' MTU is below 1500**, to that MTU:

```yaml
network:
  underlayMTU: 1460
```

- **Why:** on 1460-MTU nodes, with the default 1500, neutron advertised 1450 while only 1410 fit through the tunnel.
  Small packets and SSH worked, and full-size frames were dropped. dp303 run 4 measured this with a full-size
  don't-fragment ping (#433).
- **The refusal:** the OVS agent's init reads the tunnel NIC's MTU, the NIC it already finds from the route (above). It
  refuses to start when that MTU is below the value neutron uses for tunnels. Its message names the value to set. A NIC
  whose MTU cannot be read is refused too.
- **Examples, not requirements:** the GKE nodes dp303 ran on had NIC MTU 1460 (measured 2026-11-21), so `underlayMTU:
  1460` there. Check your VPC's MTU. Jumbo-frame underlays can raise it, up to 9216.
- **An explicit `componentValues.neutron` entry wins** (`conf.neutron.DEFAULT.global_physnet_mtu`,
  `conf.plugins.ml2_conf.ml2.path_mtu`). A standalone Neutron Composition sets those two directly. The refusal compares
  `path_mtu`, else `global_physnet_mtu`, else 1500.
- **Existing networks keep their MTU:** neutron fixes a network's MTU when it creates the network. After raising or lowering
  `underlayMTU`, change older VXLAN networks with `openstack network set --mtu <underlayMTU - 50> <network>`. Their guests
  pick the new value up when their DHCP lease renews.

`hack/validate-underlay-mtu.py` checks all of this and runs the refusal.

**Upgrading to the release that adds it:**
- **If your nodes' MTU is below 1500, set neutron's MTU through `componentValues` BEFORE you upgrade, on the release
  you are running.** `network.underlayMTU` cannot be set before the upgrade: releases before this one have no `network`
  field, so the API server drops it, the upgrade writes the default 1500, and the OVS agent refuses to start on the
  first node it reaches. That node's DHCP, L3, metadata and nova-compute agents then stay down until the value is set
  (measured on GKE, 1460-MTU nodes, upgrading 0.3.71: r074 run 2). Earlier releases do accept neutron's own settings
  under `componentValues.neutron`, and this release lets them win over `network.underlayMTU`, so set both to your nodes'
  MTU in the Openstack spec first:

  ```yaml
  componentValues:
    neutron:
      conf:
        neutron:
          DEFAULT:
            global_physnet_mtu: 1460
        plugins:
          ml2_conf:
            ml2:
              path_mtu: 1460
  ```

  Wait for neutron to be Ready on the release you are running (it restarts once for this setting), then upgrade: the
  OVS agent reads 1460 and starts. Afterwards you may move the value to `network.underlayMTU: 1460` and remove the
  `componentValues.neutron` entry; neutron receives the same spec either way, so that does not restart it.
  `hack/validate-underlay-mtu.py` part 5 checks this block: 0.3.71's schema keeps it and has no `network`; this release
  renders 1460 into neutron and its refusal check; and the spec is the same after the move.
- **neutron rolls once, on every install, even at 1500.** The first upgrade to this release adds `global_physnet_mtu`
  to `neutron.conf` and `path_mtu` to `ml2_conf.ini`; before it, neither line was rendered. Each neutron workload carries
  a hash of its configuration, so neutron-server, the RPC server and the OVS, L3, DHCP and metadata agents restart once.
- **A brief neutron API gap is expected.** neutron-server runs one replica by default, and its rollout allows one
  unavailable (#286), so the network API is briefly unavailable while it restarts. That is the rollout, not a fault.
  Later upgrades that leave `underlayMTU` unchanged do not roll neutron for it.

### Running guests on KVM

By default (`compute.virtType: qemu`) nova runs guests in QEMU software emulation. That works on any node, slowly.
To run them on KVM:

**What you need:** compute nodes that expose `/dev/kvm` to their pods, labelled `openstack-compute-node=enabled` and
`openvswitch=enabled`. Then set:

```yaml
compute:
  virtType: kvm
```

- **What kvm sets:**
  - nova gets `[libvirt] virt_type = kvm` and `cpu_mode = host-model`;
  - libvirt gets `virt_type = "kvm"`.
- **The refusal:** libvirt's DaemonSet gains an init container, `libvirt-kvm-check`, that refuses to start on a node
  without `/dev/kvm`. Its message says what to enable, with no provider assumed. The check runs in libvirt, because
  libvirtd's qemu processes are what open `/dev/kvm` when a guest starts. Without the device every boot would otherwise
  fail later, inside nova, far from the cause.
- **qemu changes nothing:** every chart renders as it always has. `hack/validate-virt-type.py` checks all of this and
  runs the refusal script.

**How compute pods land there.** nova-compute, libvirt, ceilometer-compute and cyborg-agent schedule only on nodes
labelled `openstack-compute-node=enabled`, and neutron's OVS agent and openvswitch on `openvswitch=enabled`. The
umbrella's `nodeLabelPreconditions` and `hack/validate-node-label-preconditions.py` own this. So:
- give those labels to the KVM nodes only;
- leave `openstack-compute-node` off the other nodes;
- if you taint the KVM nodes, add the matching toleration to the umbrella's `tolerations`, which reaches every pod.

**Getting `/dev/kvm`: examples.** These are examples, not requirements. Any node that exposes `/dev/kvm` works. Check
your provider's documentation for the instance types and images that support it.
- **Bare metal:** enable VT-x (Intel) or AMD-V (AMD) in the firmware and load the `kvm_intel` or `kvm_amd` module.
- **GKE** (measured 2026-11-17): create a node pool with `--enable-nested-virtualization`. N2 Ubuntu nodes in such a pool
  show `vmx` in `/proc/cpuinfo`, `/dev/kvm` and a working `KVM_CREATE_VM`; a pool without the flag shows none of these.

  ```sh
  gcloud container node-pools create compute-kvm --cluster <cluster> --region <region> \
    --machine-type n2-standard-8 --image-type UBUNTU_CONTAINERD --enable-nested-virtualization \
    --node-labels=openstack-compute-node=enabled,openvswitch=enabled
  ```
- **AWS** (not measured here): `.metal` instance types expose the host's virtualization directly.
- **Azure** (not measured here): VM sizes that support nested virtualization, such as the Dv3/Ev3 families and later.

### Driving this OpenStack with Krateo KOG operators

The KOG operators (keystone-, neutron-, glance- and nova-kog) need credentials. Never copy the admin password by hand
(#228). Opt in, and keystone renders them from the install's own generated admin root:

```yaml
kogCredentials:
  enabled: true
```

Everything lands in the Openstack release's namespace. Install the KOGs there too: the auth proxies mount
`openstack-kog-clouds`, and a Pod can mount only a Secret in its own namespace. Other namespaces aren't supported yet,
and the schema refuses `kogCredentials.namespace`.

| Object | Consumed by | What it is |
|---|---|---|
| Secret `openstack-kog-clouds` (key `clouds.yaml`) | the auth proxies of keystone-, neutron- and glance-kog | rendered by an ExternalSecret from the identity-admin root. Cloud `openstack`: keystone's internal endpoint, the admin user and project, interface `internal`, the region. |
| Secret `openstack-kog-token` (key `token`) | nova-kog (apiKey auth, no proxy) | an admin token. Keystone tokens here last 12 hours (`token.expiration: 43200`), so a CronJob, `keystone-kog-token`, re-mints it every 6 hours. A post-install,post-upgrade hook Job mints it when keystone installs or upgrades. |
| ConfigMap `openstack-kog-endpoints` (no secret) | you, when you install nova-kog | `nova_url`: nova's internal endpoint from keystone's catalog, with the admin project id from the token response appended (`.../v2.1/<project id>`). `keystone_url`: keystone's internal endpoint. `region_name`. keystone's runs write `keystone_url` and `region_name`; nova's own hook writes `nova_url` when nova installs or upgrades. |

Set each KOG to these names (read from each KOG chart's default branch, 2026-11-17):

| KOG | Setting | Its default | Set it to |
|---|---|---|---|
| keystone-kog | `authBridge.cloudsSecret` | `keystone-clouds` | `openstack-kog-clouds`. Or skip the clouds file: `authBridge.openrcSecret: keystone-keystone-admin` (0.4.1 and later). |
| neutron-kog | `authBridge.cloudsSecret` | `neutron-clouds` | `openstack-kog-clouds` |
| glance-kog | `authBridge.cloudsSecret` | `glance-clouds` | `openstack-kog-clouds` |
| all three proxies | `authBridge.osCloud` | `openstack` | leave the default. The key `clouds.yaml` is fixed: the proxy mounts the Secret at `/etc/openstack` and reads that file. |
| nova-kog | `nova.url` (chart value, required) | empty | the `nova_url` of `openstack-kog-endpoints`: `kubectl -n openstack get configmap openstack-kog-endpoints -o jsonpath='{.data.nova_url}'` |
| nova-kog | `spec.authentication.apiKey.tokenRef` on each `<Kind>Configuration` | sample: `nova-token` / `krateo-system` / `token` | name `openstack-kog-token`, namespace the Openstack release's, key `token` |

How the token and the endpoints are written:
- **A failed mint fails keystone's release, on purpose.** Each run tries at most 4 times, because keystone locks a user
  after 5 failures. When the hook Job gives up, keystone's install or upgrade fails, and so does every KOG that would
  have used a stale token. Fix the cause, then re-run the hook with `rerunHooks.keystone` (below).
- **nova writes `nova_url` itself.** Keystone installs before nova, when keystone's catalog has no compute entry yet.
  nova's chart runs a post-install,post-upgrade hook Job, `nova-kog-endpoints`. It waits for `nova-ks-endpoints` and
  nova's API, then writes `nova_url`. It runs the same program as keystone's Job, so both resolve the same URL, and
  it fails nova's release after 4 tries, as keystone's does. Once nova is in the catalog, keystone's runs write the
  same `nova_url` too.
- Only the mint step holds the admin password. keystone's writer may patch `openstack-kog-token` and
  `openstack-kog-endpoints`; nova's may patch `openstack-kog-endpoints` only. nova's step reads nova's own admin openrc
  (`nova-keystone-admin`, which nova's other keystone Jobs already read).
- Each writer sends a JSON merge patch of only its own keys: nova's names `nova_url` alone, and keystone's names
  `nova_url` only once it resolved one. So neither clears what the other wrote.
- Both objects are read back from the live cluster on every render, so a reconcile never resets them.
- `hack/validate-kog-credentials.py` checks the clouds file, the jobs and the writers' permissions. It runs the mint
  program against a stand-in keystone, and runs both writers against each other in both orders, against a stand-in
  Kubernetes API. It also checks that nothing else in the blueprint reads these objects.
- **neutron- and glance-kog read `clouds.yaml` only when their proxy starts.** After the admin password rotates,
  restart them: `kubectl -n openstack rollout restart deployment <their auth-bridge deployment>`. Until then they log
  in with the old password, and keystone locks the account after five failures. keystone-kog re-reads its Secret and
  backs off after a rejection.

**Install oasgen-provider 0.80.0 from OCI**, with its chart and CRDs in step, once per cluster, before the KOGs. Some KOG
READMEs show an unpinned install from `charts.krateo.io`, which serves 0.10.0; don't use it. The evidence for 0.80.0:
- 2026-11-17, nova-kog's owner: all 6 nova Kinds of that time had their RestDefinitions Ready and Synced on 0.80.0. This
  was a local kind cluster against an unreachable endpoint, so nothing was created. It is recorded in nova-kog's
  [docs/configuration.md](https://github.com/krateo-blueprints/openstack-nova-operator-kog/blob/main/docs/configuration.md),
  in the oasgen-provider version table.
- 2026-11-21, this repo's data-plane test, on GKE with blueprint 0.3.74-main.gc16973f: the four KOGs' 14 RestDefinitions
  were Ready, and a nova `Instance` was created, went `ACTIVE` on KVM and was deleted, against a real Nova.
- 2026-11-21, nova-kog's owner: 0.80.0 grants the controllers read access to their API-key Secret.

0.80.0 lacks the fix for a 401 during create (oasgen-provider PR #793, first released in 0.82.0); without it, a create
refused with 401 can strand the resource. So mint the KOG credentials shortly before creating resources. Before you choose any other version, check oasgen-provider's own
[retracted-versions.yaml](https://github.com/krateo-platformops/oasgen-provider/blob/main/retracted-versions.yaml). On
2026-11-21 it lists 0.53.0, 0.54.0, 0.66.0, 0.69.0, 0.70.0, 0.77.0, 0.83.0, 0.85.0 and 0.85.1.

### High availability

High availability for the stateful core is opt-in and arrives phase by phase (#309). Today:

```yaml
ha:
  enabled: true
```

- **memcached:** 3 replicas behind a headless Service, as the StatefulSet `memcached-ha`. Every component that builds
  a memcache server list names each replica, so every client hashes a key to the same replica. Without `ha`, memcached
  is one ClusterIP with ClientIP affinity: each client sticks to one replica, so several replicas would hold
  disagreeing caches (keystone's token-validation and revocation caches among them). That is why more than one
  memcached replica without `ha` is refused. Switching `ha` on starts the cache empty; nothing else is lost.
- **The node refusal:** with `ha`, the render is refused while fewer than 3 nodes carry
  `openstack-control-plane=enabled`. The replicas use required anti-affinity, one per node, so on fewer nodes the rest
  would stay Pending. The check needs the node-read grant (`node-reader-rbac.yaml`); without it, the ConfigMap
  `<release>-node-label-preconditions` says the nodes were not measured.
- **Off changes nothing:** every chart renders as it always has.

**RabbitMQ's host count has one source,** with or without `ha`: `componentValues.rabbitmq.pod.replicas.server`. Above
1, every component that lists RabbitMQ hosts gets the count, so its transport URL (and ceilometer's notification URLs)
lists every node. A component whose `componentValues` set a different count is refused. watcher names the headless
Service, whose DNS answers every ready node, so it needs nothing passed.

RabbitMQ and MariaDB move to their operators in later phases (#309). `hack/validate-ha-propagation.py` checks all of
the above against the rendered charts.

**RabbitMQ on its operator (`messaging.backend: operator`, #309 H2b), for fresh installs:**

```yaml
messaging:
  backend: operator
```

- **The broker:** a RabbitmqCluster `openstack-mq`, run by the RabbitMQ cluster-operator, which
  [krateo-blueprints/rabbitmq-operator-blueprint](https://github.com/krateo-blueprints/rabbitmq-operator-blueprint)
  installs. That blueprint needs cert-manager.
- **No release yet:** neither that blueprint nor this mode has a release until the H2 live tests have run.
- **Image:** pinned to `rabbitmq:4.3.4-management`, never the operator's default.
- **Administrator:** comes from the install's generated root.
- **Clients:** every component lists the cluster's pods and runs quorum queues, which RabbitMQ 4.x needs.
- **Users and vhosts:** instead of a rabbit-init Job, each component's RabbitMQ user, vhost, permissions and policies
  are messaging-topology-operator resources, fed by the same generated root. The vhosts are retained on deletion, and
  the policies drop the `ha-*` keys 4.x refuses. Every pod that waited on the Job waits on those resources being
  Ready.
- **Each user's password Secret is the umbrella's, and comes first.** The topology operator refuses a User whose Secret
  does not exist yet, and ESO writes a Secret seconds to a minute after its ExternalSecret (measured 3-85 s on GKE,
  H2 run 4). So the umbrella writes `<component>-rabbitmq-topology-user` (username and password from the component's
  root, labelled `rabbitmq.com/topology-operator: "true"`), and creates the component only once that Secret exists,
  labelled; until then the component is listed in `openstack-install-pending`. Its chart imports the name and renders no
  projection of its own. Run 4, before this, measured every messaging component's first install refused 1-2 times and
  retried for up to 198 s.
- **A component installed on its own, without the umbrella,** is passed no name: its chart writes the projection
  itself in the same release, and its first User create is refused until ESO has written the Secret, then admitted on
  core-provider's next retry.
- **Refusals, on the Composition's status:** the rabbitmq component refuses a cluster without the operator's CRD,
  and an install that already runs the chart-mode broker; moving one over is a separate migration (H2c).
- **Off (`chart`, the default):** nothing changes.
- **Gate:** `hack/validate-messaging-operator.py` checks all of the above.

**nova-compute's liveness checks the process only (#146).** Upstream's liveness and startup probes ping nova-compute
over RPC, so a RabbitMQ outage restarted every hypervisor's agent at once and repaired nothing. Readiness keeps the RPC
check, so the outage still shows as NotReady. `hack/validate-nova-compute-liveness.py` runs the rendered probe.

### A component reads Ready=False/HooksMayNotHaveRun

core-provider (#563, fixed in 2.13.108 by #569) can leave a component's first install `deployed` with its post-install
hooks never created. That happens when every composition-dynamic-controller loses its lease mid-install, for instance
while GKE resizes the control plane (measured 2026-11-17: cyborg's pods sat in `Init` forever). A core-provider that
carries the fix marks such a release `krateo.io/hooks-unverified`, and the component reads
`Ready=False/HooksMayNotHaveRun`. Removing that annotation by hand only stops the report. It does not run the hooks.

**From core-provider 2.13.128** an interrupted FIRST install (a release that never had a deployed revision) is uninstalled
without hooks and installed again, hooks included, at most 3 times; then the component reads `HooksMayNotHaveRun` as
before (core-provider#629). The annotation `krateo.io/interrupted-install-recovery: manual`, on the CompositionDefinition
or the composition, keeps the manual path.
- **Measured at 0.3.74 on GKE (2026-11-23):** tacker's and cyborg's controllers were force-deleted during their first
  install. Each was reinstalled once and became Ready with every hook Job Complete. Their databases were kept. Tacker's
  volume claims were deleted and created again: they belong to a release that had never been deployed.
- **An interrupted upgrade is not reinstalled.** A once-deployed tacker killed mid-upgrade was rolled back and upgraded
  again, with its claims and data kept. Its Helm history keeps the interrupted revision as `pending-upgrade` beside the
  newer `deployed` one (2.13.128): to tell whether an operation is in progress, read the latest revision only.
- **The recovery below, at 0.3.74:** step a was used to start that upgrade, and step c on cyborg (Ready about 3 minutes
  after the delete). Both components were healthy, so neither cleared a real `hooks-unverified` annotation.

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

### Upgrading to 0.3.75: APIs run two replicas, and every pod requests resources

This upgrade adds pods and starts reserving capacity (#286, #283). See "Cluster size" above for the totals. Check
that the cluster has room before you apply it:
- **Every pod is replaced once**, because its resources change. Each API rolls with the new strategy straight away:
  its second replica, and then each replacement, starts before an old pod is deleted.
- **A pod that does not fit stays Pending.** Before, every pod requested nothing, so a full cluster still accepted
  more. If a surge pod cannot be scheduled, the rollout waits, and the old pod keeps serving.
- **A stored replica default can hold a component at 1 replica.** If your `componentValues.<component>.pod.replicas`
  set some keys, the API server stored the schema's old default, 1, for that component's other replica keys when it
  created the Composition. They stay at 1 after the upgrade. Set them explicitly.
- **An explicit `spec.enabled` that lists trove or octavia now selects nova and glance too** (and, for trove, neutron), with
  what they need, such as libvirt and placement. Both services call them, and keep one Keystone session from their first
  call on (#462). A `profile` install already had them.

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

### Monitoring

`spec.monitoring` adds the **openstack-monitoring** component (#312), which sends the install's metrics and alerts to
the Krateo observability stack. It is explicit opt-in: only `enabled: true` turns it on. Unset (the default) is off,
exactly like `false`.

**Recommended when alert-provider is installed** (the Krateo installer's portal feature ships it):

```yaml
spec:
  monitoring:
    enabled: true
    otlpEndpoint: http://otel-collector-daemonset-local.krateo-system.svc:4318   # the default; any OTLP/HTTP endpoint
```

With `enabled: true` on a cluster that does not serve alert-provider's `Alert` and `Metric` CRDs, the umbrella refuses
to render and says why on its status, naming alert-provider. Its CRs would otherwise fail to apply.

- **What it scrapes.** One OpenTelemetry collector Deployment scrapes three sources and forwards them as OTLP to
  `otlpEndpoint`:
  - **openstack-exporter 2.0.0-alpha** reports per-service API availability (`openstack_<service>_up`) and nova and neutron
    agent liveness. It logs in as its own Keystone user, `openstack-exporter`, whose password is a generated root,
    `openstack-credential-identity-user-openstack-exporter`. Nova, Neutron and Placement show their services and
    agents only to an admin, so this user has the admin role on the `service` project, like every service user.
  - **A standalone mysqld-exporter v0.21.0** logs in to the `mariadb` Service as MariaDB's `exporter` user (the
    `openstack-credential-oslo-db-exporter` root). The mariadb chart's sidecar exporter is not used: its 2019 image
    gates the database pod's readiness, and switching it on restarts the only database replica.
    - The mariadb chart creates the user for any host (`krateo.exporterUser`, set by this value).
    - The grants are PROCESS, BINLOG MONITOR and SLAVE MONITOR, with at most 3 connections. It does not get `SELECT ON *.*`,
      so it cannot read the OpenStack databases.
    - Turning it on adds a Job to mariadb and does not change its StatefulSet.
  - **RabbitMQ's `/metrics/detailed`** gives per-queue depth and consumers on every server pod. The `rabbitmq_prometheus` plugin
    serving it is already enabled. It is scraped only when rabbitmq is selected.
- **Why a plain Deployment.** The collector is a Deployment, not an `OpenTelemetryCollector` CR: the Krateo
  observability stack runs no OpenTelemetry Operator (checked in krateo-platformops/installer on 2026-11-18). Its own
  collectors are deployed from the upstream opentelemetry-collector Helm chart. If the installer adds the operator, the
  collector should become a CR.
- **Cloud-agnostic.** Nothing in the defaults is cloud-specific. The endpoint is an input, and the credentials come from
  the install's generated roots through ESO.
- **The alerts.** These are alert-provider `Alert` CRs in the release namespace, the only namespace the install writes
  to. alert-provider reconciles Alerts in every namespace. They are defined in one file,
  `blueprints/openstack-monitoring/chart/files/alerts.yaml`:
  - an API down for Keystone, Glance, Nova, Neutron, Cinder or Placement;
  - a Nova service or Neutron agent down;
  - MariaDB unreachable;
  - a RabbitMQ queue that is not drained.
  - Only the alerts for selected components are rendered: an absent service's metric reads 0 and would fire forever.
  - Each alert links a runbook in [docs/runbooks/](docs/runbooks/openstack-api-down.md).
  - Every scrape job has a companion alert, `openstack-scrape-down-<job>`. HyperDX reads a window with no data as 0,
    so without the companion a dead scrape would silence the alerts that fire on high values.
  - These are metric alerts, so a firing notifies and shows on the portal's alerts page. It opens no Incident:
    alert-provider opens Incidents only for counted log records, and only in its home namespace. Putting them in
    `krateo-system` would gain nothing.
- **Off, nothing changes.** Unset or `false`, the render is byte-identical to a release without this value: no
  component, no definition, no credential root. `enabled: [openstack-monitoring]` is refused, because this value is the
  one switch.
- **Not measured yet, as of 2026-11-18.** No cluster has run this component. Phase 2 measures the rest:
  - the metric names, which are cited against each exporter's source at the pinned tag rather than scraped;
  - the default endpoint drops traffic from a node with no Krateo node agent (a tainted node, for instance);
  - how HyperDX evaluates the gauges and the grouping.

  `hack/validate-openstack-monitoring.py` is the gate.
- **Never delete** `openstack-credential-identity-user-openstack-exporter`. Like every root, it holds the only copy of
  the password, and it is kept when monitoring is switched off.

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
- [docs/images.md](docs/images.md) — every container image a chart can run (generated).
- [docs/supply-chain.md](docs/supply-chain.md) — image inventory, CVE scan, and the pinning decision.
- [docs/llms.txt](docs/llms.txt) — the version-pinned LLM doc index.

The dashboard (Horizon) logs in as `admin` with the install's generated password (domain `Default`): `kubectl -n openstack get secret openstack-credential-identity-admin -o jsonpath='{.data.password}' | base64 -d`:

| Login | Identity → Projects (admin) |
| ----- | --------------------------- |
| ![Horizon login](docs/horizon-login.png) | ![Horizon dashboard](docs/horizon-dashboard.png) |

## Develop & release

`.github/workflows/release-tag.yaml` packages every `blueprints/*/chart` and pushes each to
GHCR on a semver tag (`git tag 0.1.0 && git push origin 0.1.0` →
`oci://ghcr.io/krateo-blueprints/charts/<component>:0.1.0`).

`.github/workflows/lint.yaml` runs on every PR: the `hack/validate-*.py` gates, one file each in
`hack/gates.d` (each states the defect it was written for), the umbrella `componentValues` schema-in-sync check
(`hack/gen-componentvalues-schema.py`), the service-contract SSO deep-link resolution, the
removed-values refusal, and the shared `lint-docs` documentation-standard job. A change adds a
`docs/log.d` file and, for a new gate, a `hack/gates.d` file. A pull request never touches
`docs/log.md`, which a bot renders on main after each merge, and never adds a check step to
`lint.yaml` by hand. See [docs/release.md](docs/release.md).

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
