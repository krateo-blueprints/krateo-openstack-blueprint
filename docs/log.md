---
type: Log
title: krateo-openstack-blueprint — log
description: Curated chronological history of krateo-openstack-blueprint — notable changes and decisions, not a generated changelog.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack
tags: [openstack, log, history]
timestamp: 2026-08-11T00:00:00Z
---

# Log

Curated history; release notes live in GitHub Releases.

## 2026-11

- **Pre-release channel: verify a commit on GKE before its tag exists (#342 follows).** Every `X.Y.Z` tag reaches the
  Marketplace, so `prerelease.yaml` (dispatch, input `ref`) publishes a commit's charts to GHCR at `X.Y.Z-main.g<sha7>`.
  It attaches the stamped CompositionDefinitions, RBAC grants and examples as the run artifact `prerelease-<version>`.
  It cannot write the Marketplace index, a tag or a release: it calls no reusable workflow, uses no secret but
  `GITHUB_TOKEN`, and holds `contents: read`. The index's only writer is krateo-blueprints/charts
  `publish-chart.yaml`, fed from its caller's own run with `CHARTS_PUBLISH_TOKEN`, measured on 2026-11-17 through the
  portal and the marketplace catalog. `release-tag.yaml` and `prerelease.yaml` now run the same scripts
  (`hack/publish-charts.sh`, `hack/stage-release-assets.sh`, `hack/require-lint-passed.sh`). `hack/release-version.sh`
  refuses a version of the wrong shape before anything is stamped. `prune-prereleases.yaml` keeps the newest 5
  pre-releases and never deletes a release. It needs the repository secret `PRERELEASE_PRUNE_TOKEN`, and a manual run
  is a dry run by default. `hack/validate-prerelease-channel.py` gates all of it, with 32 mutations. Also: releases up
  to 0.3.73 carried a stray `compositiondefinition-chart.yaml` asset (cyborg's CD), now gone. The flow is in
  [release.md](release.md).

- **One file per log entry and per gate.** Every pull request edited the top of `docs/log.md` and the step list
  of `.github/workflows/lint.yaml` in the same places, so branches in flight collided on both (Diego, 2026-11-17).
  - **Log:** each entry is now `docs/log.d/<yyyy-mm-dd>-<nn>-<slug>.md` with OKF front matter, and
    `hack/render-log.py` writes `docs/log.md`, which stays checked in. The 14 existing entries were split out in their
    order, dated from the commit that added each. The rendered file differs from the old one only by two blank lines
    between entries. `hack/render-log.py --check` fails when `docs/log.md` is stale.
  - **Gates:** the 76 check steps became `hack/gates.d/<gate>.yaml`, one file each, with name, job, order, the exact
    command and its comment. Each of the 11 jobs keeps its tools and runs its gates through one step,
    `hack/run-gates.py --job <job>`. The two GitHub expressions (the job-hooks base commit, the migrate-to-pvc
    matrix) come in through the runner step's `env`. The commands CI runs, listed per job before and after, are the
    same and in the same order. The only additions are the two new gates. `hack/validate-gates-registered.py` fails
    when a `hack/validate-*` script is in no gate file, or a gate file names a script that is missing.
  - [release.md](release.md) says how to add both. `docs/log.md` and the check steps of `lint.yaml` are never
    edited by hand.

- **Provider-agnostic KVM guidance.** libvirt's `/dev/kvm` refusal no longer names a cloud provider. It says what to
  enable on any node: VT-x/AMD-V with the kvm module on bare metal, nested virtualization on cloud VMs, or
  `compute.virtType: qemu`. `hack/validate-virt-type.py` now asserts those terms, and that no provider is named. The
  README's "Running guests on KVM" leads with the generic requirement (nodes exposing `/dev/kvm`, labelled
  `openstack-compute-node=enabled`). Bare metal, GKE (measured), AWS and Azure follow as examples.

- **Image inventory and CVE scan (#316).**
  - **Inventory:** 58 distinct images across the 38 published charts (quay.io 49, docker.io 8, ghcr.io 1). Every
    chart is rendered at its defaults and with every off-by-default manifest on, and each chart's `images.tags` is
    added. All 58 are pullable anonymously as measured on 2026-11-17, gnocchi's Kolla images included (#115).
    `hack/gen-image-inventory.py` generates `hack/images.yaml` and [images.md](images.md). Offline, in `lint.yaml`,
    it fails when a chart's images differ from the files, and when an image's registry namespace has no publisher
    in `hack/image-sources.yaml`.
  - **Registry re-check:** `--resolve` asks each registry again, without credentials. It fails when an image is no
    longer pullable (v1 manifest, no linux/amd64, 401 or 404) and reports a moved digest without failing.
  - **CVE scan:** `.github/workflows/image-scan.yaml` scans every pullable image with Trivy 0.76.1, pinned by a
    digest in the workflow. It runs on a push that changes the inventory, weekly, and on each release tag. It took
    2.6 minutes of wall time for all 58. It isn't run on every pull request because only an image change or a new
    upstream CVE changes its result. A CRITICAL finding fails unless `hack/image-cve-allowlist.yaml` has an entry for
    that CVE, image and package that hasn't expired (at most 120 days ahead). The judging rules run offline per pull
    request (`--self-test`).
  - **First scan:** 177 CRITICAL findings in 43 of the 58 images. PyJWT 2.11.0 is in 29 images (25 airshipit
    OpenStack service images and the 4 Kolla gnocchi images), and `nginx:1.13.3` (Debian 9, run by ironic at defaults) has 54. All are allowlisted as a measured baseline that
    expires on 2027-02-15 ([supply-chain.md](supply-chain.md)).
  - **Pinning, open for Diego** ([supply-chain.md](supply-chain.md)): 35 images use release-series tags, and each
    airshipit one was pushed 27 to 32 times in 30 days. Proposed: dated tags with the digest recorded and verified
    in CI. Mirror the four Kolla gnocchi images, which have no immutable tag. Not `@sha256:` in the charts (#72).

- **KVM guests and KOG credentials (0.3.73, #303).** These are the blueprint's half of the data-plane release stage.
  - **`compute.virtType: kvm`:** sets nova `virt_type=kvm` and `cpu_mode=host-model` and libvirt's `virt_type`. libvirt
    then refuses, in an init container, to start on a node without `/dev/kvm`; the message names GKE's
    `--enable-nested-virtualization`. qemu, the default, renders as before.
  - **`kogCredentials.enabled`:** has keystone render, from the generated admin root and in the release's namespace,
    `openstack-kog-clouds` (an ExternalSecret clouds.yaml for the KOG auth proxies), `openstack-kog-token` (nova-kog's
    admin token) and `openstack-kog-endpoints` (nova's internal URL with the admin project id, keystone's internal URL
    and the region, as keystone answered them; no secret). keystone's post-install,post-upgrade hook Job and a CronJob
    (every 6 h) write the token, `keystone_url` and `region_name`. nova's own hook Job writes `nova_url` once nova
    registered its endpoint. Both run one shared program (`helm-toolkit.snippets.kog_endpoints_pod`), at most 4 tries
    per run, and merge-patch only their own keys. A failed mint fails that chart's release. Only the mint step holds
    the password, and each writer may patch only its own objects.
    Cross-namespace is not supported yet. The README gives each KOG's setting for these names, read from the KOG
    charts.
  - **New gates:** `hack/validate-virt-type.py` and `hack/validate-kog-credentials.py`.

- **Every long-running container has a readiness probe (#231, #151).** On a 0.3.47 cold install,
  watcher-decision-engine exited 1 seven seconds after every start, had no probe, and counted as Ready
  in each window, so its composition and the umbrella went Ready=True. 32 of 76 long-running containers
  had no readiness probe.
  - **RPC workers** (conductors, schedulers, engines, managers, agents: 30 containers in 16 charts) run
    `worker-health-probe.py`, one script in helm-toolkit (`helm-toolkit.scripts.health_probe`). It is
    OpenStack-Helm's nova and neutron `health-probe.py` made generic. It finds the program in /proc,
    limited to its own container's processes, and reads the program's own `--config-file` and
    `--config-dir` arguments. It requires an ESTABLISHED RabbitMQ connection, then sends an RPC call to
    the program's own `topic.server` queue, which only a consuming server answers. Each topic, exchange
    and server name was read from the stable/2026.1 source, including the cases that aren't
    `CONF.host`: cinder and manila `host@backend`, mistral, trove and blazar `0.0.0.0`, cloudkitty and
    ironic `getfqdn()`, magnum's random id (topic only). The RPC charts set `rpc_ping_enabled`, so a
    server answers the probe at DEBUG rather than logging an ERROR every minute.
  - **Other workers:**
    - Notification listeners (aodh listener and notifier, ceilometer-notification,
      ironic-neutron-agent) require their RabbitMQ consumer connection.
    - octavia-health-manager, octavia-driver-agent and ironic's tftp sidecar require their listening
      socket, attributed by inode.
    - mariadb-controller writes a heartbeat after every pass.
    - skyline's nginx is probed through its proxy.
    - nova-compute-ironic uses nova's own probe.
    - tacker-server gets an HTTP probe on its versions document.
  - **Readiness only for RPC workers** (#146): an RPC failure can't tell a wedged process from a
    broker outage, and a liveness probe on it restarts every replica at once and repairs nothing.
    tacker-server is the one new liveness probe. It asks `/`, which needs no dependency, and has a
    startup probe because its start script installs packages first.
  - **Exceptions:** 12 containers have no signal a probe can test (pollers, periodic loops, masakari's
    monitors, designate-sink with no handlers). Each is listed with its reason in
    `hack/readiness-probe-exceptions.yaml`.
  - **New gates:** `hack/validate-readiness-probes.py` renders every chart at its defaults and with
    every workload on (CI, 2026-11-17: 114 long-running containers in 37 charts; 53 without a readiness
    probe on main, 12 on this change, all listed). It fails when a container has no probe and no exception, when an exception is
    unneeded, when a probe is `true`, or when a probe's /tmp script isn't mounted (this caught
    tacker-conductor's). `hack/validate-health-probe.py` runs the rendered script against
    oslo.messaging's real RPC server and dispatcher, and against real processes and sockets in /proc.

- **Start ordering, and re-running hooks (0.3.72, #297, #298, #299).**
  - **#298:** ceilometer-notification now waits for the rabbit-init Job of every vhost it consumes, as the umbrella
    computes it, overrides included (`conf.notification_vhost_jobs`). Check 26b asserts the jobs equal the vhost list.
  - **#299:** manila's bootstrap waits for keystone, glance, nova and manila-api; the umbrella now creates manila after
    glance and nova. glance's storage-init and octavia's bootstrap now wait for keystone. The new
    `hack/validate-job-service-dependencies.py` maps every `openstack` CLI noun in a Job's script to its service type,
    fails on an unknown noun, and requires the Job's entrypoint to wait for that service.
  - **#297:** umbrella `rerunHooks.<component>` passes a token to that component alone, as `krateo.rerunHooksToken`,
    declared in every component schema and rendered nowhere. It forces the helm upgrade that re-runs hooks a first
    install missed. The new `hack/validate-rerun-hooks.py` checks the schema, that only that component's spec moves,
    the declaration, and that every install hook is also an upgrade hook (static).

- **Bootstrap uploads each image once; glance's store to 10 GiB (0.3.71, #295).**
  - **What happened** (0.3.68 → 0.3.70 upgrade on GKE): manila's bootstrap ran upstream's `image show || create` while
    glance-api was rolling, and read "glance unreachable" as "missing". It uploaded a second 952 MB manila-service-image,
    which filled the 2 GiB store. glance went unready on its free-space floor (#163), and glance, manila and nova
    stalled.
  - **Bootstrap fix:** glance's, manila's and ironic's bootstrap, and ironic's cleaning-network Job, now count by name
    (helm-toolkit `openstack_count`). A failed lookup fails the Job, and nothing is created when one or more already
    exist.
  - **Store:** a new `glance-images` claim requests 10Gi (`volume.defaultSize`). An existing claim keeps its size on
    upgrade (Diego, 2026-11-17), so no upgrade patches a claim nobody asked to change, and kind's local-path class (no
    `allowVolumeExpansion`) can't fail one. `volume.size`, empty by default, grows the claim and never shrinks it.
  - **New gates:** `hack/validate-bootstrap-idempotent.py` (no create after a failed lookup unless the API refuses
    duplicates; image bootstraps run against a stand-in) and `hack/validate-glance-store.py` (the size each live claim
    gets).

- **designate-worker's init container reads designate.conf.d (0.3.70, #291).** The 0.3.69 GKE upgrade never got
  designate Ready: `designate-worker-init` runs `designate-manage pool update`, which reaches designate-central over RPC,
  and it mounted `designate.conf` alone. 0.3.69 had moved `transport_url` into `designate.conf.d`, so the `[ -d ]` guard
  dropped `--config-dir` and oslo.messaging dialled localhost:5672 forever. The init container now mounts the pod's own
  drop-in volume, as the worker does. The gate had left init containers out on a premise that was false for this chart
  (that they share their pod's drop-in volume): checks 13, 22 and 22m now treat an init container that starts an
  OpenStack program as a reader, and new check 19c requires every such init container, in every chart and rendered mode,
  that mounts its project's main config to mount its `.conf.d` too. The survey found no other one.

- **Per-service RabbitMQ users and ceilometer's vhosts (0.3.69, #228, #285).** The last #228 tier: 22 services' own
  RabbitMQ users still logged in with OpenStack-Helm's published password. Each username now has a generated root,
  `openstack-credential-oslo-messaging-<user>`, passed only to the chart that renders it (no username is rendered by
  two charts, measured: `hack/mq-users.yaml`). The transport URLs leave the base config for a drop-in,
  `88-transport.conf` (helm-toolkit `transport_dropin`), separate from the database one because the two reach
  different containers: only programs that send or receive over RabbitMQ get it, measured per program from the
  stable/2026.1 source (`hack/mq-readers.yaml`). neutron's agent health probes gained `--config-dir`: they parse the
  config without a project and, with no transport URL, report healthy unchecked. `<svc>-rabbitmq-user` becomes an
  ExternalSecret. rabbit-init needed no change: `rabbitmqadmin declare user` is an HTTP PUT that replaces an existing
  user's password (measured on `rabbitmq:3.13-management`). #285: the umbrella passes ceilometer only the notification
  vhosts a selected component creates (`components[].rabbitmqVhost` and `rabbitmqInit`, measured from the renders),
  under the names it creates them, following each component's componentValues (`manifests.job_rabbit_init`,
  `endpoints.oslo_messaging.path`; Diego, 2026-11-17), instead of upstream's fixed seven. Gates: 20 no longer exempts the messaging sections, 20b covers per-service RabbitMQ URLs at every host,
  and new checks 15c (map and one root per username), 22m (drop-in fidelity, delivery to exactly the readers, probes),
  23m (rabbit-init run against a stand-in rabbitmqadmin) and 26 (ceilometer's vhosts per profile). Check 24b no longer
  counts db-init, db-drop and the bootstrap Job as cell-mapping loaders: they run no nova code.

- **Umbrella `tolerations` (0.3.68).** One list on the Openstack resource reaches every pod of every component, as
  OpenStack-Helm's `pod.tolerations.<component>`. A trusted-tenant vcluster copies the pool taint
  `krateo.io/trusted-tenant=<ns>:NoSchedule` onto its nodes, so without it every DaemonSet desired 0 pods. Nine charts
  had no `pod.tolerations` block (cyborg, designate, magnum, masakari, mistral, octavia, powerdns, redis, watcher), and
  workloads and helm-toolkit Jobs in others never applied it. `hack/validate-tolerations.py` renders every chart with
  every workload manifest on and requires exactly the list on every pod template.

- **Per-service MariaDB users (0.3.68, #228).** All 28 services' own MariaDB users still logged in with
  OpenStack-Helm's published password. Each MariaDB username now has a generated root,
  `openstack-credential-oslo-db-<user>`, passed only to the chart that owns it. nova's three databases and octavia's
  persistence database are one account each, so they get one root at several keys (`extraPaths`). The connection URLs
  leave the base config for a drop-in, `87-db-connection.conf` (helm-toolkit `db_dropin`). `<svc>-db-user` and its
  siblings become ExternalSecrets. Horizon reads its password from a file, and skyline's `skyline.yaml` carries both
  roots. db-init now runs `ALTER USER ... IDENTIFIED BY` after `CREATE USER IF NOT EXISTS`, so a changed password
  reaches an existing user; that is to be proposed upstream. Diego accepted the rotation window (2026-11-16); the
  upgrade run measures it. Gates: check 20 no longer exempts the database sections, and 20b fails a mysql URL that logs
  in as a per-service user with a published password. New checks: 15b (the map `hack/db-users.yaml`), 22 (drop-in
  fidelity and delivery, least privilege included) and 23 (db-init and db-drop run against a stand-in SQLAlchemy).
  nova's cell mappings now store URL templates that each process formats from its own config, so `nova_api` holds no
  password: nova caches each cell's connections per process (CELL_CACHE), and a pod that started before db-sync
  rewrote a literal URL kept the old password until it exited (check 24). neutron's agents no longer receive the
  database URL at all; only the servers and database tools do (`hack/db-readers.yaml`, check 22).

- **ceilometer's notification URLs (#281).** Since 0.3.57 the notification agent logged in to seven RabbitMQ vhosts
  as the administrator with the published password, which the server refuses. The URLs are now derived from
  `endpoints.oslo_messaging` and `conf.notification_vhosts`. With a generated admin root they reach only
  ceilometer-notification, through a drop-in. Check 20b (`hack/validate-credentials.py`) fails any URL that logs in as
  an install-wide administrator with a published password. It also found nova's `nova-db-api-admin` and
  `nova-db-cell0-admin` and octavia's `octavia-persistence-db-admin`, Secrets nothing read that carried the
  published MariaDB root password; they are no longer rendered.

- **Contract emitter removed (0.3.68).** The CMP's single emitter of the `serviceContract`
  usage and health is `krateo-platformops/cmp-usage-collector`, which reads each OpenStack
  from the parent (Diego, 2026-11-16). An emitter in this chart would double-count usage next
  to it. `contractEmitter.enabled: false` stays accepted for older Compositions; anything else
  is refused (`hack/validate-removed-values.sh`). The deep-link resolution step, lost from CI
  on 2026-08-11 while the docs still claimed it, runs again.

## 2026-08

- **Docs standard adopted.** Added the OKF documentation bundle (this `docs/` set +
  `docs/llms.txt` + `examples/identity-profile/README.md`), rewrote `README.md` to the
  six-section shape, and wired the shared `lint-docs` job into `.github/workflows/lint.yaml`.

- **Dead-org sweep.** Eliminated the legacy dead-org references in favour of
  `krateo-blueprints` (part of the krateo-platformops/installer standardization).

## Earlier

- **Orchestrator umbrella.** Added `blueprints/openstack` (Kind `Openstack`): one Composition
  registers every component `CompositionDefinition` and emits each component `Composition`
  once its CRD exists and its dependencies report `Ready=True` — an ordered, readiness-gated
  rollout that avoids the all-at-once `create-pending` race. `spec.enabled` selects an
  explicit component set expanded to its transitive dependency closure.
- **Service contract + contract emitter.** The orchestrator carries a `serviceContract` v1
  stanza (identity federation, role mapping, capabilities → aggregation ClusterRoles / Kyverno
  ABAC policies, SSO deep-link, usage/health/tagging/ticketing). Added the runtime
  `contractEmitter` (CronJobs polling OpenStack → ClickHouse), default-off.
- **Compute + bare-metal planes.** Verified the compute plane (OVS/Neutron VXLAN, Nova on
  libvirt+QEMU, CirrOS VM to `ACTIVE`) on amd64 GKE, and shipped the production-grade `ironic`
  blueprint (ipmi/redfish, iPXE/TFTP/HTTP, conductor on `hostNetwork`). Two determinism fixes
  were required for the Krateo CDC path.
- **One blueprint per chart.** Established the design: one `CompositionDefinition` per
  OpenStack-Helm chart, each vendoring the upstream chart (2025.1 "Epoxy", `ubuntu_jammy`)
  with Helm hooks stripped and a curated `values.schema.json`. Identity plane verified
  end-to-end on kind and GKE.
