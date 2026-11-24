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

- **barbican and gnocchi move to `ghcr.io/krateo-blueprints/images/`** (#454): upstream's builds minus compile-time Debian
  packages, published by the image-build run on 4e06df70.
- **All 75 `linux-libc-dev` exceptions are gone.** The five PyJWT ones move with the images, same expiry: no Python package
  changed.
- **gnocchi is pinned for the first time (#334).** It ran Kolla's series tags, which are rebuilt in place; the 17 November build
  we had recorded was no longer on quay. The chart now runs that build, from our mirror, under a dated tag, so the four pin
  waivers go. This moves gnocchi back from whatever Kolla had rebuilt since to the recorded build.
- **The mirror list is empty:** nothing it lists is run any more. Its tags stay published.
- **Not run on a cluster yet.**

- **The failure:** the merge queue dropped #471, which doesn't touch powerdns, because the server "did not pass readiness after
  the outage with a NOTIFY queued". It had failed no earlier lint run among the last 60.
- **The test's gap:** it started the server again only if it had exited before the database was back. A server that exits
  in the seconds after that look (it exits when its communicator thread can't reach the database) failed the test at once,
  because the wait gives up when the server is gone. The kubelet restarts it. The failed log doesn't record the server's state, so that this is what happened isn't proven.
- **The change:** the 45 s wait restarts an exited server and counts it, and a failure now prints the restarts, the state and
  the last log lines. The limit and what must pass are unchanged.

- **The problem (#454):** barbican's image and the four Kolla gnocchi images carry `linux-libc-dev`. Every kernel CVE is a
  finding in it with no fix published: 15 new CRITICAL findings on 2026-11-23, 35 on the 24th, each blocking image changes
  and release tags. It is header files; nothing in the images runs it.
- **Diego's decision:** remove the package instead of excepting it batch by batch.
- **`hack/image-build.yaml` gains a second kind of image:** upstream's own build, at the digest the scan measured, minus an exact
  list of Debian packages. `dpkg --purge` has no network and no resolver, and refuses while anything else depends on a listed
  package. The result must have upstream's Debian packages minus the list and the same Python packages, still import its
  service, and keep user, entrypoint and PATH.
- **Gnocchi's four are built from our mirror:** quay no longer serves the recorded digests, because Kolla rebuilds its series
  tags in place. `hack/image-mirror.yaml` holds the same digests.
- **What goes:** `linux-libc-dev` and what depends on it, all compile-time (`libc6-dev`, the `-dev` packages, g++ in gnocchi's):
  8 packages of 211 in barbican, 18 in each gnocchi image. The lists come from the scan's own package data, which misses a
  dependency on a virtual package: dpkg refused gnocchi's first list, without `libpcre2-dev`.
- **This change only builds them.** The charts move in the next one, and the dated exceptions go with it.

- **The failure:** raw.githubusercontent.com answered 503 (`Backend.max_conn reached`) to a gate's only request in #458, #324
  and #469, on 2026-11-23 and 24. Each failed a capability-roles job that had nothing to do with the change.
- **The fix:** `hack/fetch.py`, used by `hack/constraints.py` and `hack/validate-operator-admission.py`. 429, 5xx and network errors are
  tried again, five times in all over 73 s. A 404 fails at once, and the last error is raised.

- **masakari and cloudkitty move to `ghcr.io/krateo-blueprints/images/{masakari,cloudkitty}:2026.1-ubuntu_noble-20261124`**
  (#463), published by the image-build run on b2852aa2 and pullable anonymously. Compared with upstream's 20261117 builds
  they add cryptography 43.0.3, cffi and pycparser, and a newer certifi; no package is lost, and user, entrypoint and PATH
  are the same.
- **The PyJWT exceptions move with them** (CVE-2026-102268): OpenStack's 2026.1 upper constraints pin 2.11.0, so a build under
  them carries it too. Same expiry, 2027-02-15.
- **The guard:** `hack/check-image-packages.py` runs with the image scan, which now lists every package. A chart that sets
  `memcache_security_strategy: ENCRYPT` may not run an image that has keystonemiddleware without cryptography.
- **Not run on a cluster yet:** the two images, and a token cached with ENCRYPT by them.
- **Kernel-header findings:** this branch's first scan found 7 new CVEs in `linux-libc-dev` in barbican's and the four gnocchi
  images (35 findings, no fix published), which this change doesn't touch. Diego's decision: rebuild those images without
  the package (#454). Until then they get dated entries to 2026-12-23.

- **The defect (#463, r074g, 0.3.74 on GKE, 2026-11-23):** masakari answers 500 to every request with a valid token and
  cloudkitty closes the connection. Their charts set `memcache_security_strategy: ENCRYPT`; their images lack `cryptography`
  (`CryptoUnavailableError`), in the pinned 20261117 builds and in 20261123's. With `MAC` instead, both answered 200, so the
  missing package is the whole cause.
- **Diego's decision:** build the two images here, from upstream's Dockerfiles, with their own `EXTRA_PYTHON_PACKAGES` argument
  set to `keystonemiddleware[memcache_encryption]`. Encryption of the token cache stays on.
- **`hack/image-build.yaml`** pins the Dockerfiles' commit, the two base images by digest and the source commits. The
  Dockerfile clones a branch, so the build refuses to run when the branch has moved, and checks the commit the image
  records.
- **`.github/workflows/image-build.yaml`** builds and verifies on a pull request with no write permission, and publishes on main
  only, to dated tags it never moves.
- **This change only builds the images.** The charts move to them in the next one, once the tags exist and the scan has
  seen them.

- **The defect (#462, r074g, 0.3.74 on GKE, 2026-11-23):** trove answered 500 to every instance list, while its component read
  Ready. Two causes, both measured:
  - trove's clients default to `publicURL`, and nothing serves the public endpoints while `publicEndpoints` is off (#451);
  - trove-api started 14 minutes before nova's endpoints were registered. Trove keeps one Keystone session for its process's
    lifetime, so it kept a catalog without nova.
- **The same default was in five more charts, read from their stable/2026.1 source.** A list request doesn't exercise any of
  them, so none was seen failing: masakari's nova client, magnum's seven clients, designate's neutron client, octavia's,
  and mistral's actions.
- **The fix:**
  - all six set the internal endpoints;
  - left public on purpose: heat's `clients_heat`, magnum's `magnum_client` and trove's swift, whose URLs are handed to
    instances outside the cluster;
  - trove depends on glance, neutron and nova; octavia, whose clients are class attributes, also on glance and nova. So
    `spec.enabled: [trove]` now selects nova and what nova needs.
- **Verified:** trove's five options on the r074g cluster (through `componentValues`): 200 with the admin token, 401 without a
  valid one. The other five charts are rendered, not run; octavia is not in full-strict.
- **The guard:** `hack/validate-internal-clients.py` requires the 27 options in the rendered config, rejects any other client
  rendered public without a reason, and checks the dependencies of the services with a long-lived session. The list of
  options is explicit, because the defaults live in the images and no render shows them.

- **2.13.128's automatic reinstall is exercised on our charts (r074g, released 0.3.74, full-strict, 3-node GKE, 2026-11-23).**
  - **The kills:** the tacker and cyborg composition controllers were force-deleted while an install hook Job was active,
    with the release at v1 `pending-install` and later hooks not created.
  - **The result:** one `InterruptedInstallReinstalled` event each, one v1 deployed release, every hook Job Complete with a
    new UID, Ready, and no annotation left. A marker row in each database survived.
  - **Could the row have been lost?** Cyborg ran with `manifests.job_db_drop: true`, and an ordinary delete of its
    Composition then did drop the row. Tacker's hook is off by default, so its row proves nothing.
  - **What is lost:** tacker's 3 claims were replaced. core-provider's next release is to stop uninstalling.
  - **The event overstates:** "deleted 7 hook Job(s)" counts the hooks named in the release record (confirmed by
    core-provider from its code).
- **The control: a once-deployed tacker killed mid-upgrade was not reinstalled.** Helm's history is v1, v2
  `pending-upgrade`, v3 `Rollback to 1`, v4 deployed; claims and marker kept, hooks run again. The upgrade was started
  with `rerunHooks.tacker`, its first use at 0.3.74. The script reported FAIL on its event count, 1 to 0. The first event
  had expired. One 50 minutes old was still listed, and none was newer.
- **#459 diagnosed.** trove: #462. masakari and cloudkitty: #463. blazar, freezer and magnum were the probe's own requests.
- **The README says all of it**, with the stale `pending-upgrade` record 2.13.128 leaves behind.

- **APIs keep running through a rollout; every container is sized (#286, #283).**
  - **Why:** the instrumented 0.3.67 → 0.3.68 upgrade lost keystone token issue (9 of 76 samples), placement,
    orchestration and metric. Each API ran 1 replica with `max_unavailable: 1`. Separately, 33 of the 36 charts left
    `pod.resources` off, so every pod was BestEffort.
  - **Replicas and disruption.** Every Deployment that a Service selects (31, in 27 charts) now runs ≥ 2 replicas and
    rolls with `max_unavailable: 0` / `max_surge: 1`. All of it uses OpenStack-Helm's own keys: `pod.replicas`,
    `pod.lifecycle.upgrades.deployments.rolling_update` and `pod.lifecycle.disruption_budget.*.min_available: 1`.
  - **PodDisruptionBudgets.** New budgets for skyline, powerdns, nova-novncproxy and cyborg-conductor. The new
    `helm-toolkit.utils.pdb_min_available` lowers a budget below the replica count, so an override to 1 replica never
    blocks a drain. Eight upstream budgets protected nothing:
    - designate's six selected `app: designate-<service>`, a label no pod carries. Five of them had no other label,
      so dropping it alone would have selected every pod in the namespace. They now select by helm-toolkit labels.
    - cloudkitty's and masakari's sat in a misnamed `pbd-api.yaml` and selected component `<service>_api`, while the
      pods carry `api`. They are renamed `pdb-api.yaml` and fixed.
  - **Spread over zones.** helm-toolkit's anti-affinity gains a preferred zone term (`pod.affinity.anti.zone`), and
    skyline gains the anti-affinity it lacked.
  - **The exceptions are decided by the render.** glance-api and tacker mount ReadWriteOnce claims, so they stay at one
    pod and must not surge. tacker now uses Recreate: Kubernetes' default would have stalled a rollout on Multi-Attach.
    The reasoned exceptions are in `hack/single-replica-serving.yaml`: redis, whose second replica would be a second,
    empty store, and the two metrics exporters, which are scrape targets and would publish every series twice (Diego,
    2026-11-23).
  - **Watcher's decision engine** read `pod.replicas.engine` (null), so the documented `decision_engine` key reached
    nothing. cyborg's and swift's schema replica defaults (1) disagreed with their values (3).
  - **Requests:** `pod.resources.enabled: true` in all 36 charts. Every container gets requests and a memory limit,
    with no default CPU limit:
    - Turning the switch on was not enough. These containers still rendered no sizing, because nothing in values
      reached them: the kubernetes-entrypoint init container every pod runs (helm-toolkit), 41 template containers
      with no resources include, and keys the templates read but values never defined (designate's five workers,
      neutron-rpc-server, memcached's `server`, and others). 0.3.73's KOG credential Jobs and CronJob
      (`pod.resources.jobs.kog_token` and `jobs.kog_endpoints`) and libvirt's kvm-check init container are sized too.
      They render only behind a switch, so `hack/feature-renders.yaml` declares the values that turn each one on, and
      both gates render them.
    - The pre-delete hook-cleanup Job (#206) that 33 charts render through helm-toolkit is sized from
      `pod.resources.jobs.hook_cleanup`, like every other Job. openstack-monitoring (#312) sizes its
      kubernetes-entrypoint init containers and drops the CPU limits its exporters, collector and ks-user Job carried.
    - The figures are OpenStack-Helm's own unless a reason is written next to them, because no per-container usage
      has been measured. `full-strict` now requests 9.8 CPU and 14.6 GiB for the control plane, plus 1.3 CPU and
      1.9 GiB per node (README, "Cluster size").
  - **Gates:**
    - `hack/validate-api-availability.py`: serving Deployments ≥ 2 replicas with maxUnavailable 0; RWO-pinned ones one
      pod with no surge; every multi-replica workload has a budget that keeps one pod and allows a drain, including
      with every replica key at 1; no budget empty or selecting nothing; spread over hosts and zones; integer
      replicas; schema defaults equal values.
    - `hack/validate-pod-resources.py`: every container and init container is sized, at defaults, with every
      workload manifest on, and with each feature in `hack/feature-renders.yaml`.
    - Both render cinder and manila through `hack/gate_values.py`, as the other chart gates do: each refuses to
      render with no backend chosen (#196, #426).
    - On main they fail in 28 and 36 charts:
      - 29 serving Deployments below 2 replicas, and 30 that drop a pod to roll;
      - 11 budget defects, and 3 multi-replica workloads that do not spread;
      - 808 of 837 containers unsized.
    - 24 mutations, each caught.

- **The install block's `BP_VERSION` moves from 0.3.71 to 0.3.74**, tagged 2026-11-23 at ed476f37. The README lists the
  runs it rests on (r074, 3-node GKE, full-strict) and what they did not cover:
  - a fresh install of the tag commit on core-provider 2.13.111, and of the released 0.3.74 on 2.13.128;
  - the 0.3.71 to 0.3.74 upgrade on 2.13.111 only, with the MTU set through `componentValues` first (#452);
  - 2.13.128's automatic reinstall (core-provider#629) was not exercised: GKE did not resize the control plane in that run.
- **The install block says where `network.underlayMTU` goes.** The published example is `profile: identity`, which runs no
  neutron. A reader who switches profile on nodes below 1500 was not told, in the block, that 0.3.74 refuses to start the
  OVS agent without it.
- **Two lines left the "not yet fixed" list:** the fixes for #298 and #299 shipped in 0.3.72 (8a59a1fe). #299 stays open: its
  fix has not been witnessed on a cluster since, and manila left full-strict in 0.3.74. #449 and #451 joined the list.
- **Read while writing it: seven APIs never answered a request successfully (#459),** on five GKE runs from 0.3.71 on, while
  their components read Ready. The upgrade check judged only services with a 2xx baseline, so it excluded exactly these.
  The README says so. #451 is corrected too: with `publicEndpoints` off none of the 22 public endpoints is served.

- **The image scan turned red on the 0.3.74 candidate (image-scan run 45098584294, main 5bc3e765, 2026-11-23).** The
  morning's scan of the same commit was green. 35 CRITICAL findings had been published since.
  - **20 in `docker.io/bitnami/kubectl`:** curl and curl-libs 8.22.0, 10 CVE/BDSA ids, fixed upstream in 8.23.0. libvirt
    declares this image for its `cert-init-vnc` init container, which renders only with `conf.qemu.vnc_tls: "1"`. The
    default is `"0"`, so the image is `declared-only` in the inventory.
  - **15 in `linux-libc-dev`:** CVE-2026-99287, -99306 and -99310, in barbican's and the four kolla gnocchi images.
    Trivy reports no fixed version.
- **Diego's decision: fix where a fix exists, allowlist only what has none.**
  - **The fix:** libvirt's `images.tags.kubectl` is pinned to `bitnami/kubectl@sha256:ae71f848…`, the `latest` index Bitnami
    published 2026-11-22T20:44Z. The previous digest, `bedd34c9…`, was pinned on 2026-10-30 (#70). This pull request's
    image scan scans the new digest; it must be free of the curl findings.
  - **The allowlist:** `hack/image-cve-allowlist.yaml` gains 15 dated entries for `linux-libc-dev`, expiring 2026-12-23.
    It is the kernel's userspace headers, which no process in those images runs. Remediation is tracked in #454.
- **For 0.3.74:** only the libvirt chart changes. The tag moves to this commit, after a new pre-release and the fresh-install
  run, with chartdiff showing libvirt as the only changed chart.

- **The README's MTU upgrade step was impossible (found by r074 run 2, 0.3.71 -> 0.3.74-main.geb42756 on GKE).**
  - **What the note said:** "set `network.underlayMTU` before you upgrade".
  - **Why it can't work:** 0.3.71's Openstack schema has no `network`, and its top level is not preserve-unknown. The API
    server therefore drops the field from the live object. The upgrade's new apiVersion then writes the schema default,
    1500.
  - **What the run measured:** the OVS agent's init refused on the first node it reached (`ens4 has MTU 1460, below the
    underlay MTU 1500`). Its DHCP, L3, metadata and nova-compute agents waited in init, and neutron and nova stayed
    `Creating`.
  - The run's harness had also missed the note (it read the previous commit's README). Setting `network.underlayMTU:
    1460` afterwards was logged as intervention 1.
- **The fix (Diego: pre-set via componentValues):** the README now tells an operator on sub-1500 nodes to set
  `componentValues.neutron.conf.neutron.DEFAULT.global_physnet_mtu` and `conf.plugins.ml2_conf.ml2.path_mtu` on the
  release they are running, before the upgrade.
  - 0.3.71 keeps both: `componentValues.neutron` and neutron's `conf` are preserve-unknown.
  - 0.3.74 lets them win over `network.underlayMTU`, and the refusal compares the merged `path_mtu`, so the OVS agent
    starts.
  - The value can later move to `network.underlayMTU`: neutron receives the same spec, so that does not restart it.
  - Fresh installs set `network.underlayMTU` as before.
- **The guard:** `hack/validate-underlay-mtu.py` part 5 parses the README's block and checks it against 0.3.71's
  schemas, frozen from the tag in `hack/fixtures/`.
  - **The premise:** 0.3.71 has no `network` and prunes unknown top-level fields.
  - **Kept:** the block's paths are preserve-unknown, and helm accepts the block.
  - **The new release:** neutron's spec, `UNDERLAY_MTU` and `check_underlay_mtu` against a NIC of that MTU.
  - **The move:** neutron's spec is equal after it.
  - **A misspelt block:** must be caught.
- r074 run 3 tests this procedure on a real upgrade before 0.3.74 is tagged.

- **Manila's catalog entry outlived it (#444, found by the 0.3.71 -> 0.3.74 upgrade test r074).**
  - **What happened:** the upgrade removed manila, but its Keystone service `sharev2` (`manilav2`) and its internal and
    admin endpoints stayed in the catalog, pointing at `manila-api`, a Service deleted with the release. manila's
    install Job pods stayed too.
  - **The consequence:** Skyline's `skyline-nginx-generator` writes a literal-host `proxy_pass` per catalog endpoint
    with no `resolver`, so nginx refused to load its config and crash-looped. Skyline never went Ready, and so the
    umbrella never did.
  - **Why the removed chart can't clean up:** `helm uninstall` runs the hooks of the deployed release, manila's
    0.3.71 chart, which deregisters nothing. No change to a removed chart reaches an install upgrading from an older
    release.
- **The fix (Diego's design, 2026-11-22), in two parts:**
  - **Deregistration, from the new release:**
    - **When (a state machine, Diego's design):** on every reconcile the umbrella looks up, by name, each
      not-selected component's Composition and every Service its chart renders. While any exists the component is
      `removing`: nothing is deregistered, and `openstack-deregistration-pending` names what remains. Once none exists
      it is `gone`, and the umbrella passes keystone, as `krateo.deregister`, its Keystone services (minus any type a
      selected component registers) and its name. Nothing is remembered between reconciles, so the order of the
      removal and keystone's upgrade doesn't matter.
    - **A component run on its own is never deregistered:** a standalone Manila next to the umbrella keeps its
      `manila-api` Service, so it stays `removing`.
    - **Where the list comes from:** it is derived from the charts, not kept by hand. `hack/gen-keystone-registrations.py`
      renders each chart's ks-service Jobs into `osh.keystoneRegistrations`, and each chart's Services into
      `osh.componentServices`.
    - **The hook:** keystone's new post-install/post-upgrade Job, `keystone-deregister`, deletes each such service
      with its endpoints, only when type and name both match. It also deletes those components' leftover hook Jobs,
      with background propagation.
    - **Idempotent:** it covers upgrades from any older release and a later deselection by a user.
  - **A tolerant Skyline:** after the generator, skyline's init rewrites each endpoint location to resolve per request
    through the cluster DNS. The `resolver` comes from the pod's resolv.conf. The URI semantics are unchanged: the
    rewrite to the endpoint path plus a variable host. A host that does not resolve now answers that location with
    502, and nginx starts. Upstream divergence `skyline-444`.
- **The guards:**
  - **`hack/validate-deregistration.py`:**
    - the table is current, manila registers sharev2, and no type has two owners;
    - keystone's list for each profile, a must-fail check on #444's catalog, and the catalog and jobs programs run
      against stand-ins;
    - the state machine, with its lookups stood in: gone (handed over), removing (the Composition exists) and
      standalone (only `manila-api` exists), both kept;
    - 6 mutants, including the Service lookup and the Composition check each removed.
  - **`hack/validate-skyline-nginx.py`:** upstream's template is rendered with an unresolvable host. `nginx -t` fails
    before the rewrite and passes after it, and a running nginx proxies the same path and answers the dead host 502.
    3 mutants.
- **Upgrading:** nothing to do. The first upgrade to this release runs keystone's hook, which removes what earlier
  removals left. A Keystone Composition whose migration is not approved (pinned) receives no `krateo.deregister`
  (README).

- **A controller-only change rolls the database (#445, found by r074 on the 0.3.71 -> 0.3.74-main.gc16973f upgrade,
  where it was one of two causes of the MariaDB restart).**
  - **Measured:** the server StatefulSet got a new revision at 18:34:26Z, the one MariaDB pod was replaced at
    18:34:48Z, and Keystone's token API failed from 18:34:23Z to 18:36:18Z. The upgrade's Keystone-user Jobs retried
    until it was back.
  - **Cause, two changes (a field-by-field render diff, 0.3.71 against this release):**
    - the server's pod template carried upstream's `configmap-bin-hash`, a hash of the whole rendered `mariadb-bin`.
      That ConfigMap also holds `mariadb_controller.py`, which only the controller Deployment runs, and #335 added its
      readiness heartbeat. This is #445;
    - the pod template also carries hashes of the credential ExternalSecret manifests (dbadmin, sst, dbaudit), which
      #422 moved to `refreshPolicy: OnChange`. That part is not changed here: a new root must still roll the server,
      and hashing only what decides the projected data is #449 (0.3.75).
    - RabbitMQ rolled at the same upgrade for the same #422 reason (admin and Erlang-cookie ExternalSecrets) and for
      #343's dated init-container image.
- **The fix:** `templates/_bin_hash.tpl` hashes, per workload, only the `mariadb-bin` keys it mounts, read back from
  the rendered ConfigMap itself. The server hashes `start.py`, `stop.sh` and `health.sh`, and the controller
  `mariadb_controller.py`. Upstream divergence `mariadb-445`.
- **The guard:** `hack/validate-mariadb-bin-hash.py`, in job-hooks. It appends a line to every `templates/bin/` source
  of a rendered key, one at a time, with defaults and with backup on. Every long-running workload mounting
  `mariadb-bin` must change its pod template exactly when a key it mounts changed, and each mutation must reach the
  ConfigMap. The planted control restores upstream's shared hash on the server and must be caught on the controller's
  script.
- **Upgrading:** the first upgrade to 0.3.74 restarts the MariaDB and RabbitMQ servers once each, on every install, for
  the reasons above; the new annotation value is part of that one restart. Later changes to scripts the server does not
  mount no longer restart it (README "Upgrading to 0.3.74 or later: MariaDB and RabbitMQ restart once").

- **VXLAN networks advertised more than the tunnel carried (#433, found by dp303 run 4 on 0.3.74-main.gc16973f).**
  - **Measured:** on nodes whose NIC MTU is 1460, neutron gave a VXLAN network MTU 1450, its default 1500 minus the
    50-byte VXLAN overhead. A full-size DF ping from the router's namespace to the guest got
    `Frag needed and DF set (mtu = 1410)`: only 1460 - 50 fit. Small packets and SSH worked, so nothing else showed it.
    A guest sending full 1450-byte frames, for example a bulk TCP transfer without working path MTU discovery,
    stalls or drops them.
  - **0.3.74 shipped with it as a known issue (Diego).**
- **The fix (Diego's design, #433):**
  - **The input:** `network.underlayMTU` on the umbrella, an integer from 1280 to 9216, default 1500. It is always
    passed to neutron, as `conf.neutron.DEFAULT.global_physnet_mtu` and `conf.plugins.ml2_conf.ml2.path_mtu`, so a
    VXLAN network advertises underlayMTU - 50. An explicit `componentValues.neutron` entry wins.
  - **The refusal:** the OVS agent's init already finds the tunnel NIC from the route. It now reads that NIC's MTU and
    refuses to start when the MTU is below the one neutron uses for tunnels (ML2 `path_mtu`, else
    `global_physnet_mtu`, else 1500). The message names `network.underlayMTU: <the NIC's MTU>` and the MTU that would be
    advertised, with no provider assumed. An unreadable MTU refuses too. Upstream divergence `neutron-433`.
- **The guard:** `hack/validate-underlay-mtu.py`, in capability-roles. It checks five things:
  - the umbrella's span for every component: only neutron receives the value, 1500 when absent, and componentValues
    wins;
  - the schema's bounds;
  - the rendered `neutron.conf` and `ml2_conf.ini`, the files neutron reads, for 1460 and 1500;
  - the init's `UNDERLAY_MTU` for standalone values;
  - the refusal function, extracted from the rendered script and run against a fake `/sys/class/net`.

  Three mutants are all killed: the comparison removed, the call removed, and the umbrella's merge removed.
- **Upgrading:** on nodes whose NIC MTU is below 1500, set `network.underlayMTU` to it before upgrading; otherwise the OVS
  agent refuses to start there. Networks created before the change keep their MTU (README "neutron's underlay MTU").

- **A rotation stopped in minting (H2 run 5, defect H, 2026-11-19).** The README procedure
  (`rotate.oslo-messaging-heat: <token>`) minted the generation root `openstack-credential-oslo-messaging-heat-g<8 hex>`,
  and the umbrella rendered its projection. The projection then stayed `SecretSyncedError` ("could not get secret data
  from provider"): the SecretStore's identity, `<credentials.store>-reader`, may `get` only the Secrets its Role lists by
  `resourceNames`, and the list held only `credentials.items`. The new User was never created, so nothing switched;
  heat kept working on generation 0.
- **The fix:** one helper, `osh.generationRoots`, lists the generation roots a render may read: each `rotate.<id>`
  token's root, and the root stamped on it as its predecessor (`krateo.io/previous-root`), which a second rotation still
  drains. The reader Role (`templates/credentials.yaml`) and the drift latch (`osh.credentialDrift`) both read that one
  list, so the grant and the latch cannot disagree about which roots exist.
- **Why the gates missed it, and the guards now:**
  - The rotation gates stood in for ESO, and a stand-in cannot see a missing RBAC grant (the same lesson as #393 and
    #397: rendered is not admitted).
  - `hack/validate-messaging-rotation.py` part 2 now compares the rendered Role's `resourceNames` with every root a
    rendered projection reads through the store, while minting and during a second rotation that drains the first; a
    mutant dropping the generation roots from the Role fails it.
  - `hack/validate-eso-onchange-switch.py` step 7 measures it on the real ESO 2.14.0 in kind: a store whose Role has the
    umbrella's exact shape (get by resourceNames) but omits a root refuses that projection with `SecretSyncedError`;
    with the root added, the same projection syncs with no change of its own. So the list is what decides, and the
    render check measures something real.
- **Verified:** locally, the rotation gate (9 umbrella checks, 8 mutants killed) and the ESO gate (steps 0 and 3–7);
  live, the P3 re-test of the README procedure is H2 run 6.

- **manila-share never served a share (#426, found by dp303 run 3 and H2 run 5 on 0.3.74-main.ga0e21bf).**
  - **The cause:** upstream's default backend, `generic` (`GenericShareDriver`,
    `driver_handles_share_servers = true`), dies in `init_host` on `FileNotFoundError: [Errno 2] No such file or
    directory: 'ip'`. Its service-instance helper runs `ip`, and no airshipit manila image ships iproute2.
  - **Measured on three images,** without running them: the one 0.3.73 ran, the last jammy pin (0.3.23) and 0.3.74's.
    The binary is absent from the image, not just from PATH, and the same method found dpkg, tr and os-release, so the
    absence is a measurement.
  - **Why it looked fine before:** manila-share had no readiness probe until 0.3.74 (#335, #412). The process stayed
    alive under its name, so it reported `Ready`. The new probe reports "no ESTABLISHED connection to RabbitMQ",
    truthfully, and an install that selects manila never reaches `Ready`.
  - **Also wrong by design:** the generic driver serves shares from Cinder volumes on Nova service instances, which an
    install with `storage.backend: none` cannot give it.
- **The fix (Diego: Manila requires a share backend, Cinder's pattern from #196):**
  - **The profile:** manila's umbrella entry requires `shared-filesystem`, which nothing provides yet (CephFS comes with
    #308), so `profile: full-strict` leaves it out: 31 components, not 32. `profile: full` and `enabled:` still select
    it, and it then refuses to render, as cinder does with no backend.
  - **The chart:** `krateo.shareBackend` defaults to `none`, which refuses, naming what to set. `unmanaged` lets an
    operator configure their own backends under `conf.manila`; any backend whose `share_driver` is the generic driver
    is refused, since it cannot start on these images.
  - **The other gates:** they render manila through `hack/gate_values.py` with one operator example
    (`hack/share-backend-operator-example.yaml`, a CephFS-native backend), so its probes, config files and drop-ins are
    still measured rather than its refusal.
- **The guard:** `hack/validate-share-backend.py`, in capability-roles. It checks that every refusing chart requires
  `shared-filesystem`, and that nothing provides it; that the chart refuses at its defaults, with unmanaged plus
  generic, with an empty backend list, with a backend with no section and with an unknown value; that the operator
  example renders a manila-share workload, and its generic-driver twin is refused; and two mutants (each refusal
  removed), both killed. The preconditions file gains manila's entry, so `hack/validate-provisioning-preconditions.py`
  holds `requires` and the declared precondition to each other.
- **Upgrading:** a `full-strict` install upgraded to 0.3.74 uninstalls its manila Composition. That manila never served
  a share.

- **The rotation completes (#309 C2, Diego's (d)).** `rotate.oslo-messaging-<service>` used to mint only the next root
  (C1). Now the umbrella carries the rotation through, each step derived from what exists in the cluster:
  - **Minting:** the umbrella projects the new root, as `<service>-rabbitmq-topology-user-g<8 hex>` with user
    `<service>-g<8 hex>`. Once that Secret exists, labelled, the service's release renders a second User and
    Permission on the same vhost (helm-toolkit `endpoints.oslo_messaging.operator.users`).
  - **Switch:** once the new User is Ready, the service's passwordFrom and username name the new generation.
  - **Draining:** the previous User stays until every reader has rolled (`osh.generationRolled`), then leaves the
    release.
  - **Reporting:** `status.credentialRotation` lists rotations in progress, and ConfigMap
    `openstack-credential-rotation-<id>` shows the phase and what each waits on.
- **A switch did not roll every reader.** Measured on all 20 rotating charts:
  - **Before:** 9 workloads in 6 charts mount a projection of the root, but their pod template did not change when
    the root and username changed: aodh-api, cloudkitty-api/-processor, cyborg-api, designate-mdns/-producer,
    masakari-api/-engine, nova-novncproxy. They would have kept the old credential and been locked out when the old
    user was deleted. The rest rolled only because their entrypoint waits named the User.
  - **The fix:** helm-toolkit `custom_pod_annotations` adds `krateo.io/messaging-generation: <active suffix>`, absent
    until a first rotation, so an upgrade rolls nothing.
  - **Templates that lacked the snippet:** 15 workload templates didn't include it, so they get it: blazar-manager,
    ironic-/magnum-conductor, mistral-engine/-event-engine, neutron-rpc-server, tacker-conductor/-server,
    watcher-api/-applier/-decision-engine, and the aodh, designate and nova (x2) cleanup and cell-setup CronJobs.
- **"Rolled", and its sources:**
  - **The release, hooks included:** the component Composition's `status.observedGeneration` has caught up with its
    generation, and no `krateo.io/hooks-unverified` is set.
    - Read at the version core-provider 2.13.111 deploys (krateo-platformops/core-provider tag 2.13.111, commit
      7cd0848). It writes observedGeneration only in setStatus (`support.go:377`; unstructured-runtime v1.27.1
      `statusprojection.go:177`), after a successful reconcile.
    - That reconcile runs one real helm upgrade whenever anything changed, and the upgrade runs and waits for the
      hooks (plumbing v1.95.0 `helm/v3/reconcile.go`, steps 5-8).
    - So hook Jobs, which aren't in `status.managed`, need no clock. Upstream composition-dynamic-controller `main`
      writes no observedGeneration; that was the first measurement, against the wrong source.
  - **Workloads:** each Deployment, StatefulSet, DaemonSet and CronJob carries the new generation and passes kubectl
    v1.36.4's rollout predicates (`rollout_status.go` :75-87, :107-112, :130-146). A partition counts as not rolled
    while any pod is below it. OnDelete, bare Pods and unreadable objects are kept and reported, never forced.
  - **Jobs:** no Job running, CronJobs included.
  - **Projections:** the service's projections of the new root have synced at their own generation.
- **Entrypoint waits:**
  - **Own service:** a pod waits on its own service's **active** User and Permission. Without this, a pod restarting
    after the generation-0 user's deletion would have waited forever.
  - **Another service:** a pod waits only on that service's vhost and the administrator's permission (ceilometer's
    notification agent waited on four services' Users). The umbrella passes `operator.serviceUser` for this, and at
    0.3.74 ceilometer-notification rolls once for it.
- **Drift:** the latch also covers generation roots, by Secret name.
- **Guard:** `validate-messaging-rotation` (capability-roles 492):
  - 34 step cases with the lookups stood in, from heat's own `status.managed`. They cover each step, the
    second-token wait, the deletion of the old User, a release not yet applied, hooks-unverified, and half-finished
    rollouts for every kind and failure.
  - 7 umbrella render checks: the generation projection, the report, the span with and without a rotation, and drift.
  - All 20 rotating charts: one user per generation, waits on the active one only, every reader rolls, no non-hook
    Job changes, the readers are exactly `osh.messagingReaders`. With generation 0 gone, a restarting pod waits on
    the active user, and generation 0's User is not rendered.
  - 7 mutants killed, the annotation-removed one naming that chart's workloads.
- **One live read per component** (`validate-two-phase-values`): the read is reached only when holding, when a token
  is set, or when a generation root of the component is latched in `openstack-credential-generations`. That marker
  keeps a removed token refused without a GET on every render; it costs one ConfigMap read per render in
  `compositions.yaml`.
  - **With that ConfigMap absent** (lost, or an install from before it existed), the state is unknown, so that render
    reads every existing rotating component live. A removed token is still refused, and the render re-creates the
    ConfigMap, so the next render reads nothing again.
  - **Counted:** 0 plain, 1 rotating, 1 with the token removed after a rotation, 20 with the ConfigMap absent (one per
    rotating component), 36 holding, 36 holding and rotating.
  - **The 0.3.74 upgrade** meets an absent ConfigMap once, so its first render makes those 20 reads.

- **Editing a root was an uncoordinated rotation (#309 C1).** H2 run 4's P3 patched a RabbitMQ root, and External
  Secrets Operator regenerated it within a second. v2.14.0 skips a refresh only while the Secret's data matches its
  data-hash annotation (`externalsecret_controller.go:370`, `isSecretValid`), under Owner whatever the refresh policy.
  RabbitMQ's projection then moved at once and heat's transport drop-in an hour later, and heat was refused 1894 times.
  Any hourly projection carried such an edit into one side of a credential while the backend kept the other.
- **Projections stop following roots:**
  - **The change:** every projection of a generated root is `refreshPolicy: OnChange` with no interval: 25
    templates, 13 in helm-toolkit, 12 in component charts, plus the umbrella's messaging projections. The Ceph copies
    stay Periodic 5m, since their owner rotates them by design.
  - **Measured on ESO 2.14.0 in kind** (`validate-eso-onchange-switch`, operator-admission job):
    - the switch leaves the Secret byte-identical;
    - OnChange ignores a root change that a 10 s Periodic twin follows;
    - it refreshes on its own change, follows a root rename, and syncs once a missing root appears.
  - **Nothing relied on in-place changes:** every release-driven rotation (0.3.53-0.3.68) introduced a new root, so
    its projection was new or its spec changed.
  - **Guard:** `validate-pod-references` checks 270 projections and plants an hourly one.
- **A hand-edited root is reported:**
  - **Detection:** the umbrella latches a sha256 of each generated root (ConfigMap
    `openstack-credential-generations`); ESO's own SHA3-224 data-hash can't be computed in a template.
  - **The report:** a change is listed on `status.credentialDrift` by Secret name, with the current sha in ConfigMap
    `openstack-credential-drift-<Secret>`.
  - **Acknowledging it:** `credentialDrift.acknowledge.<Secret>` takes that sha, and only that sha.
  - **Guard:** `validate-credential-drift`.
- **Rotation mints a new root.** `rotate.oslo-messaging-<service>`, with `messaging.backend: operator` only, typed for
  the 20 messaging ids that rotate, names a new root, `<base>-g<first 8 hex of sha256(token)>`, minted once.
  - **Why not OnChange roots:** switching the existing roots to OnChange would itself be a spec change, and ESO would
    regenerate every root at the upgrade.
  - **Derived from what exists:** the active root is what the live Composition references, and the previous one is
    stamped on the new root's ExternalSecret, so no ConfigMap is needed.
  - **Refused, naming why:** a reused token, a removed token, and a token in chart mode.
  - **Guard:** `validate-credential-rotation`.
  - The second user, the switch and the cleanup are #309 C2.

- **Gates install image libraries at the images' versions.**
  - **What happened:**
    - oslo.service 4.9.0 reached PyPI on 2026-11-18. It initialises the eventlet backend on import, so main's
      worker-probe gate, which installed oslo.service unpinned, failed on every pull request until #412 pinned it.
    - Two other gates pinned versions the images don't ship. validate-horizon-backchannel-tests pinned Django 5.2.19
      and cryptography 46.0.5, but the horizon image ships 4.2.28 and 43.0.3. validate-nova-compute-liveness pinned
      psutil 7.0.0, but the nova image ships 7.2.2.
  - **The fix:** the three gates install with `pip install -c "$(python3 hack/constraints.py url)" <names>`. That is
    openstack/requirements stable/2026.1's upper-constraints at commit ecafdea7, kept in
    `hack/openstack-constraints.yaml` with the `git ls-remote` command that resolved it. Every transitive dependency is
    then the images' own too.
  - **Versions:** every constraint equals the version measured in the 2026.1-ubuntu_noble-20261117 images:
    - oslo.service 4.5.1, cotyledon 2.2.0, setproctitle 1.3.7, oslo.messaging 17.3.2, eventlet 0.40.4;
    - oslo.config 10.3.0, kombu 5.6.2;
    - Django 4.2.28, cryptography 43.0.3, psutil 7.2.2.
  - **Checks:**
    - Each gate first runs `hack/constraints.py check <names>`, which fails when a name is missing, unconstrained or
      at another version.
    - The worker-probe gate also runs `constraints.py self-test oslo.service`. It installs oslo.service unconstrained
      in a throwaway venv, gets 4.9.0, and requires `check` to refuse it.
    - The 72 back-channel logout tests pass on Django 4.2.28.
  - **New gate `hack/validate-pip-constrained.py`:** a gate that pip-installs anything but PyYAML must use the
    constraints, carry no version of its own, and start its run with `check` on exactly those names. Run against
    main's old gate files, it fails all three.

- **The first-User race is gone by construction (#309 H2, Diego's option 4).** H2 run 4 on GKE measured every
  messaging component, 15 of 15, refused on its first install by the topology operator's User webhook (`secret
  "<svc>-rabbitmq-topology-user" not found`), 1-2 times each.
  - **The cause:** its chart submitted the projection ExternalSecret and the User in one helm transaction, and ESO
    wrote the Secret 3-85 s later.
  - **The cost:** CDC's retry admitted the User up to 198 s after the Secret existed, and the refusal stayed on the
    status for minutes.
  - **Ruled out:** a helm hook (helm v3.20.2 waits for no ExternalSecret or Secret), the operator itself (v1.20.5 has
    no ordering, only `ENABLE_WEBHOOKS=false` or generated credentials), and a `lookup` of the root (the password
    would land in the release).
- **The change:**
  - **Who writes it:** with `messaging.backend: operator`, the umbrella renders `<component>-rabbitmq-topology-user`
    for each selected component whose rabbit-init runs, from its root, labelled for the operator.
  - **When the component is created:** only once `osh.lookupSecret` finds that Secret, labelled
    (`osh.messagingProjectionReady`, beside the dependency and Ceph gates; an existing component is never held).
  - **The chart:** it imports the name it is passed (`endpoints.oslo_messaging.operator.userSecret`) and renders no
    projection. A standalone install keeps the chart-rendered one and CDC's retry.
  - **One rule:** `osh.runsRabbitInit` is the rabbit-init rule, read by both the notification vhosts and the
    projection.
- **The guards:**
  - `validate-messaging-topology.py` requires the umbrella as the one writer, with the chart's root, key and username,
    and the label.
  - `validate-messaging-operator.py` part 5 renders the gate with a stand-in for `osh.lookupSecret`. helm template's
    lookup returns nothing, so an offline render emits no gated component. It covers 4 cases, plus the creation
    condition's structure.
  - `validate-ceph-consumer.py` check 30 now asserts the creation gate's structure, not its text.
  - Mutants killed: the gate dropped, the label ignored, `$cephReady` dropped.

- **The pull-request gates measure from an explicit base.**
  - **What was wrong:** `hack/validate-log-untouched.py` and `hack/validate-no-skip-markers.py` inferred where a pull
    request's change starts from the shape of HEAD. A two-parent HEAD was taken for GitHub's merge ref and measured
    from its first parent. A branch head that has merged main is also two-parent, but with the branch as its first
    parent, so main's merged commits were charged to the branch. Run on #412's head, the log gate failed for main's
    render of `docs/log.md`, which the branch never touched. The skip-marker gate would have read an old render's skip
    marker the same way.
  - **The fix:** both gates measure from the merge base of HEAD and an explicit base, through one helper,
    `hack/pr_range.py`.
    - **pull_request:** the base is `origin/$GITHUB_BASE_REF`. That is exact for the merge ref, the pull request's
      head, and a head that merged main.
    - **workflow_dispatch:** the base is `origin/main`, declared.
    - **By hand:** `--base <ref>` is required, or `LOG_BASE=<ref>` for a run through `hack/run-gates.py`, which
      passes a gate no arguments. A pull_request run ignores `LOG_BASE`. With neither, the gates fail rather than
      guess. A local `run-gates.py --job job-hooks` also needs that job's workflow-env variables to exist; with them
      set empty and `LOG_BASE=origin/main`, all 26 of its gates pass.
    - **merge_group and push** keep their fixed BASE (`merge_group.base_sha`, or the previous head). A missing BASE
      now fails, where it used to fall back to `origin/main`.
  - **Tests:** throwaway repositories cover a pull_request merge ref, the pull request's head, a branch head that
    merged main, and a merge group. Each case runs with `docs/log.md` left alone and with it edited (log gate), or
    with and without a skip marker (skip-marker gate). Restoring the old parent-count inference fails the
    merged-branch cases in both gates. Four base cases: neither given is refused, `LOG_BASE` alone, `--base` over
    `LOG_BASE`, and a pull_request ignoring `LOG_BASE`.

- **The worker readiness probe works on titled processes (0.3.74 defect B, #231).** 0.3.74 is blocked on this. The
  probe is new in it (#335), and no released version ever ran it.
  - **What happened** (GKE, 0.3.74-main.g0e23f7a): 14 RPC workers stayed Running but never Ready, each with
    `not ready: no <program> process is running in this container`. The services affected: designate, heat, mistral,
    neutron-rpc-server, cyborg, cloudkitty, aodh's listener and notifier, and manila-share. Their RPC worked.
  - **The cause:** the 2026.1 images ship setproctitle. Two things call it:
    - oslo.service's threading backend, which builds a cotyledon ServiceManager under both of its launchers;
    - cotyledon itself, which aodh, ceilometer and cloudkitty use directly.

    It rewrites each process's `/proc` cmdline into one title, such as `designate-central: master process [...]` and
    `designate-central: ServiceWrapper worker(0)`, and its environ with it.

    The probe found the program by an argv element's basename, and read its `--config-file` from that argv, so on
    titled services it found nothing. The eventlet backend sets no title, which is why blazar and others passed.
    Rosetta (kind on Apple Silicon) sets the title but doesn't rewrite cmdline, so it hides the defect. The probe's
    tests used plain argv, which is why CI passed.
  - **The fix:**
    - The program is now the container's processes: the probe's mount namespace, minus the probe. `--process` only
      labels messages. Readiness is still the RabbitMQ connection plus the RPC answer.
    - The configuration is passed to the probe, never read back. Each program's ordered list lives in one place,
      `<chart>.config_args.<program>` in its chart, with its template conditionals. `helm-toolkit.snippets.oslo_config_args`
      renders it into both the start script's exec line and the probe's command.
    - `--config-dir-if-present` is the probe's form of the scripts' `$([ -d D ] && echo --config-dir D)`.
    - ironic-conductor's `${OPTIONS}` files moved into its list.
  - **Proof the refactor changes no program's arguments:** this covered all 18 probed charts, rendered before and
    after:
    - at their defaults;
    - as the umbrella delivers them in identity, full and full-strict;
    - with variants that flip every conditional in the lists.

    In all 163 rendered start scripts, everything outside the program's invocation is byte-identical. Bash evaluated
    each invocation with every conf.d present and then absent: 326 argv identical, except aodh's `--config-file=X`,
    which became `--config-file X`.
  - **New gate `hack/validate-probe-config-args.py`:** every probe's list must equal its exec line's, evaluated by
    bash, in each chart as the umbrella delivers it. It runs two positive controls.
  - **Probe tests:** `hack/validate-health-probe.py` now starts real processes through oslo.service's threading
    backend (one and two workers), cotyledon directly, the eventlet backend, and a plain process, at the image
    versions. It checks ready and not ready end to end. It fails, never skips, on a host that doesn't rewrite cmdline.
    It kills three mutants: argv name matching, config read from argv, and the mount-namespace filter dropped (real
    with `sudo unshare -m`, otherwise a stub it names).

- **gnocchi was never Ready with ha.enabled (#309 H2, run 4).** On GKE, 0.3.74-main.g0e23f7a passed every messaging
  predicate of p0, but gnocchi-api failed readiness: `coordination backend memcached-discovery://memcached-ha-0…:11211,
  memcached-ha-1…,memcached-ha-2… is unusable: NoMatches: No 'tooz.backends' driver found, looking for
  'memcached-discovery'`. metricd, statsd and ceilometer waited behind it, and the umbrella never reported Ready.
  - **The cause:** upstream's `configmap-etc.yaml` took the tooz driver from `endpoints.oslo_cache.hosts.default`,
    which is right only while that host is literally `memcached`. H0/H1's `osh.haValues` (#344) sets it to the headless
    Service `memcached-discovery`, and the statefulset turns the URL into a host list.
  - **Wrong in principle too:** sack locks spread over memcached shards would let two metricd hold one sack.
  - **The fix:** with `storage.driver: redis` (the default), `coordination_url` is left unset, so gnocchi coordinates
    on the metric store's redis (`gnocchi/service.py prepare_service`). Any other storage driver gets a literal
    `memcached://` on one host, and a memcache host list is refused.
  - **A behaviour change without HA as well:** coordination moves from memcached to redis. Redis is already gnocchi's
    storage and incoming store, so this adds no new failure mode. That redis is a single replica, and in the ha
    profile too; that is #411.
- **The guard, in `validate-ha-propagation.py` part 3:**
  - every `coordination_url` and `backend_url` rendered with the ha spec must name a driver tooz 8.1.0 registers
    (2026.1 upper-constraints) and exactly one backend;
  - gnocchi's effective lock backend (its coordination_url, else its redis_url) must exist;
  - two controls: run 4's URL, planted, must be refused, and gnocchi with a non-redis store under the ha spec must
    refuse to render.
  - **Why it was missed:** before, the gate counted run 4's URL as a valid memcache list, because it compared only the
    hosts. The divergence entry `gnocchi-309-coordination-backend` asserts three lines, each failing on upstream's
    template.

- **Phase 2 of `validate-operator-admission.py`: the operators' admission webhooks in CI's kind (#405).** Three H2
  live runs on 2026-11-18 each stopped on an admission-class defect that every offline gate passed: the CRD schema
  (#393), a duplicate identity (#397), and the topology operator's User webhook (#404). `operator-admission` covered
  the first class; phase 2 covers the third.
  - **Setup:** in the same kind cluster, after the CRD-only phase, it installs cert-manager v1.21.3 (URL and sha256
    pinned in `hack/operator-crds.yaml`) and both operator manifests whole, from the pinned bytes the CRDs come from.
    It waits until both webhooks answer.
  - **The Secrets:** for every ExternalSecret the messaging gates render, it creates the Secret ESO would write, using
    `hack/eso_model.py`. That is one function: target name, type, labels and annotations, plus ESO's managed label,
    and the template's keys or spec.data's, with placeholder values. Its self-test runs first, against ESO's
    documented target semantics, and it refuses the forms it does not model (dataFrom, templateFrom, mergePolicy
    Merge, creationPolicy Merge or None).
  - **The check:** every rabbitmq.com object is dry-run again with the webhooks active. Two retries, 5 s apart, cover
    only a Secret not yet in the operator's cache.
  - **The controls,** each refused for its own reason:
    - a User importing a Secret without `rabbitmq.com/topology-operator` ("must have label");
    - a User importing an absent Secret ("not found");
    - a RabbitmqCluster whose override sets `hostNetwork` ("hostNetwork is not permitted").
  - **The limit:** the Secrets are eso_model's, so a check built on them cannot validate ESO itself. The H2 live runs
    remain the check on that part, and on everything after admission.

- **The topology operator refused every User (#309 H2, run 3).** On GKE, 0.3.74-main.g6d894b3 admitted the topology
  CRs live for the first time. Vhosts (11/11), Policies (3/3) and the administrator's Permissions went Ready. Every
  User was refused by the operator's webhook: `secret "heat-rabbitmq-topology-user" must have label
  rabbitmq.com/topology-operator=true to be used by the Topology Operator`. The user Permissions then waited on
  "missing User".
  - **The cause:** messaging-topology-operator v1.20.5 caches only Secrets carrying that label (`cmd/main.go:281-283`,
    to bound memory). Its User webhook refuses an import Secret that exists without it, or doesn't exist
    (`internal/webhook/v1beta1/label_validation.go`). Its controller watches Secrets through the same cache
    (`internal/controller/user_controller.go:385`), so an unlabelled projection would also never re-sync a rotated
    root. The projection's ExternalSecret set no labels, and ESO wrote only its own.
  - **The fix:** the projection sets `spec.target.template.metadata.labels: rabbitmq.com/topology-operator: "true"`,
    which ESO stamps onto the Secret.
  - **The guard:** `validate-messaging-topology.py` asserts the label on every projection.
  - **Why the gates missed it:** #393's admission gate validates against the CRDs and runs no operator webhook. The
    class guard, the operators' real webhooks in CI's kind against every operator-mode object, is the next pull
    request.

- **The delivered pass of `validate-pod-references.py` now shows and requires what it rendered (#309 H2, after
  #397).**
  - **Why:** #397 added a pass that renders each component as the umbrella delivers it, per profile and per selection,
    for `hack/render_identity.py`. It printed no line per render, so a log could not show that ceilometer, refused at
    #397's first head with a spec no install delivers to it, rendered under the profiles that select it. Nothing
    failed when a selected component had no delivered render.
  - **Visibility:** each delivered render prints `OK <component> (as the umbrella delivers it, profile …): identity
    only`.
  - **Completeness:** the gate fails when a component some profile selects has no successful delivered render.
    Storage-dependent charts are rendered per storage.backend instead.
  - **The planted case:** `GATE_PLANT_DROP_DELIVERED=<component>` skips that component's delivered renders, as a
    future change to the modes could. `hack/gates.d/validate-pod-references.yaml` runs the gate on ceilometer alone
    with it, and requires rc ≠ 0 and the line naming ceilometer, not merely a failure.
  - **A mutant, not a bypass:** while the variable is set, the gate exits non-zero whatever it found ("PLANTED MODE …
    can never pass"). Set by mistake in CI, it fails instead of passing on less. The repo-wide rule for environment
    reads in gate scripts is issue #398.

- **Charts pushed through the shared chart-push (charts rule 14).** `hack/publish-charts.sh` pushed each chart with
  `helm push` in a blind three-attempt retry loop. charts 1.4.0's rule-14 check scans only workflow files, so it missed
  this.
  - **The push:** `release-tag.yaml` and `prerelease.yaml` now locate the shared push.sh with a `chart-push` step,
    pinned to `krateo-platformops/.github` 1.0.0 (`e1d3e99`).
    - That chart-push directory is byte-identical to `653ac1e`'s, and its push.sh's sha256 is `e2912643…7b03`.
    - They pass `CHART_PUSH` and the registry credentials to the publish step.
  - **The script:** it pushes each chart once with `bash "$CHART_PUSH"`, and a failure stops the release.
    - push.sh retries only GHCR's tag race, and only after the registry serves the digest.
    - The script refuses to stamp anything when `CHART_PUSH` is unset.
  - **The gate:** `hack/validate-prerelease-channel.py` found release-tag's push step by the text `helm push`, which
    the migration removed. It now finds the step by its `hack/publish-charts.sh <channel>` invocation, with shell
    comments stripped, and fails when there is none. It also checks the pinned `chart-push` step, the publish step's
    `CHART_PUSH` and credentials, and that no step runs `helm push`.
    - A stand-in run checks that `$CHART_PUSH` is called once per chart, that one failure is not retried, and that a
      missing `CHART_PUSH` stamps nothing.
    - 59 mutants are caught, among them the push step removed and the push step reduced to a comment.

- **Every messaging client failed to install in operator mode (#309 H2, run 2).** With #393 the RabbitmqCluster was
  admitted and three brokers came up Ready on GKE (0.3.74-main.ge82f577). Then heat, neutron, designate, aodh and
  every other client Composition stayed Synced=False: chart-inspector answered 500 with `rendered manifest contains
  duplicate resource external-secrets.io/v1/ExternalSecret/openstack/heat-rabbitmq-user`.
  - **The cause:** `helm-toolkit.manifests.rabbitmq_topology` (#382) named the user projection the User imports
    `<serviceUser>-rabbitmq-user`. That is also the name charts give their own connection Secret,
    `secrets.oslo_messaging.<service>`, which `helm-toolkit.manifests.rabbitmq_secret` renders as an ExternalSecret of
    the same name with a passwordFrom root. That Secret still renders in operator mode for its other readers. Both
    ExternalSecrets also targeted one Secret with `creationPolicy: Owner`, so even without helm's refusal they would
    have fought over it.
  - **Why every gate was green:** `helm template` does not refuse duplicate resources; only `helm install` does.
    `validate-messaging-topology.py` took the first ExternalSecret of the imported name. #393's admission gate
    dry-runs one object at a time and sees only rabbitmq.com objects.
- **The fix:** the projection is `<serviceUser>-rabbitmq-topology-user`: the ExternalSecret, its target Secret, and the
  User's `importCredentialsSecret`. The User keeps `<serviceUser>-rabbitmq-user`, the name every wait references. The
  template refuses to render when `secrets.oslo_messaging.<service>` equals the projection's name.
- **The guards:**
  - **`hack/render_identity.py`:** no two resources of one render may share group, kind, namespace and name. The gates
    that already render every chart pass their output through it, rather than render the charts again:
    - `validate-pod-references.py`: every component chart at its defaults, under each storage.backend it depends on,
      and, new, as the umbrella delivers it: for each profile, the spec compositions.yaml builds for each component
      that profile selects (`umbrella_spec.selected`, new), each distinct spec rendered once. The selection matters:
      ceilometer's spec under identity, which does not select it, has no notification vhosts, and the chart refuses
      it by design (#281).
    - `validate-messaging-topology.py`: every messaging client in operator mode.
    - `validate-messaging-operator.py`: the rabbitmq chart (3 replicas and 1) and every client in operator mode.
  - **The ledger:** each gate reports the renders and resources checked, and fails when either is 0. Once per gate, a
    planted duplicate of the first resource rendered must be found.
  - **One owner:** `validate-messaging-topology.py` also requires exactly one ExternalSecret of the imported name and
    no other writer of its Secret.
  - **2c:** heat with `secrets.oslo_messaging.heat` set to its projection's name must refuse, with that message.

- **The docs-rendering screen's fixture contract with nova-kog.** The nova peer couldn't copy this repository's
  `hack/check-doc-rendering.py`, so it implemented the tier ordering itself (its #187). The two repositories now agree
  on five named cases instead of on identical bytes. All five passed against main's file (f260b87) before they were
  added, and both repositories now run them as self-test arms 7a–7e:
  - **7a:** uniqueness beats length.
  - **7b:** a concatenation C = A + B leaves A and B at tier 2 and C shared, at tier 3.
  - **7c:** a document quoting only some of another's tokens isn't a superset of it, which the arm asserts first. The
    quoted document takes the token the quote lacks, at tier 1.
  - **7d:** a superset means containment, and an empty token set has no supersets.
  - **7e:** a document with no tokens falls to tier 4.

- **The operator-mode RabbitmqCluster was refused by its CRD (#309 H2).** H2's live test on GKE installed
  0.3.74-main.g92cd983 with `messaging.backend: operator`. The rabbitmq Composition then stayed Ready=False with
  `RabbitmqCluster.rabbitmq.com "openstack-mq" is invalid: spec.override.statefulSet.spec.template.spec.containers:
  Required value`, so no broker was ever created, and every component that waits on messaging waited behind it.
  - **The cause:** the template's override PodSpec held only a `nodeSelector`. The cluster-operator's CRD types that
    field as a whole PodSpec, where `containers` is required.
  - **The fix:** the override lists one container, `rabbitmq`, by name only. The operator merges the override into
    its own PodSpec strategically, containers by name (v2.23.2 `internal/resource/statefulset.go`, `patchPodSpec`),
    so the entry changes nothing else. Without any entry the override would marshal `containers: null`, which the
    merge would read as deleting them; the CRD refuses that case before it can happen.
  - **Why every gate was green:** `validate-messaging-operator.py` and `validate-messaging-topology.py` render the
    objects and check this repository's own predicates. Nothing asked the CRD.
- **The guard: `hack/validate-operator-admission.py`, in a new lint job `operator-admission`.**
  - **The CRDs:** read from rabbitmq-operator-blueprint at the commit of the operator build H2 runs (e22d03a), with a
    sha256 per upstream manifest (`hack/operator-crds.yaml`). There is no copy here to drift.
  - **The objects:** the two messaging gates write every rabbitmq.com object they render from the umbrella's operator
    spec to `OPERATOR_OBJECTS_DIR`, never one from a mutation. Both gates must pass, and each required kind
    (RabbitmqCluster, User, Vhost, Permission, Policy) must have at least one object.
  - **The validator:** a kind cluster serving those CRDs dry-runs every object server-side. The API server validates,
    not a schema library, which would be a second implementation that disagrees at the edges.
  - **The controls:** per kind, one admitted object is broken and must be refused: the RabbitmqCluster without its
    override `containers` (this defect), each other kind without the first field its CRD requires.
  - **Also:** `validate-messaging-operator.py` asserts the override lists exactly `rabbitmq`, and the rabbitmq
    divergence entry asserts the three lines.
  - **Out of scope:** the operators' admission webhooks and what the operators do after admission. The H2 live test
    covers those.

- **No shell line pipes a producer into `grep -q`.** Under `set -o pipefail`, `producer | grep -q pattern` fails
  when grep exits on its match while the producer still has output to write: the producer takes SIGPIPE (rc 141)
  and pipefail makes that the pipeline's status, so a line that was found reads as missing.
  - **Where it came from:** nova-kog's 0.1.9 release refused its own chart this way (#183, 2026-11-18): the file it
    looked for was the third of 24 archive entries, so tar still had most of its listing to write.
  - **Here:** one instance, `hack/smoke-migrate-to-pvc.sh`'s second `migrate prepare`. It now captures the output,
    then matches with `<<<`, and a failing prepare fails as such rather than as "did not recognise".
  - **Same class, other readers:** `| head` (the GHCR push script's `tar -tzf | head -n1` died 20 of 20 times on a
    20,001-entry archive) and `| grep -m N`. The guard refuses all three, and refuses a small `printf | grep -q`
    deliberately: it is safe only while the output fits one pipe write, which a scan cannot know.
  - **The guard:** `hack/validate-no-pipe-into-grep-q.py` (job-hooks) scans every workflow and shell file. It fails
    on scanning nothing, and its self-test must catch four planted forms and pass four safe ones.

- **The docs-rendering screen identifies each document past its own concatenations (#388).** `docs/log.md` is rendered
  from every `docs/log.d` entry, so it repeats all of their tokens. Counted against everything, it left 40 of 71
  documents identified only by a shared token. That proves the response wasn't truncated, but not that it was for that
  document.
  - **Tiers:** each document takes the first tier that yields a token (agreed with the nova peer):
    1. the longest token unique across all documents;
    2. else the longest token unique once the documents containing this one's whole token set (its concatenations)
       are left out;
    3. else the longest shared token, reported as shared;
    4. else any block element.

    Every document is still checked.
  - **Why per document:** the rule's first form (18056b1) left every superset out of one global census. Four
    documents that merely quote small entries count as supersets that way: `README.md`, `docs/configuration.md`,
    `docs/release.md` and `docs/supply-chain.md`. Leaving them out for everyone handed 17 documents a "unique" token
    that one of those quoting documents' renders also held.
  - **Measured on 72 documents:** tier 1: 32, tier 2: 34, tier 3: 6, tier 4: 0.
    - Unique tokens that another, non-concatenation document also holds: 17 before, 0 after.
    - Still shared, as before: `docs/log.md` and the 2026-11-16-01 entry.
    - Now shared, honestly: four whose "unique" token was quoted elsewhere (the 2026-11-18-02 and -31 entries,
      `_earlier.md`, `examples/identity-profile/README.md`).
  - **What no token can fix:** an entry whose every token a quoting document holds (8 here, for example 2026-11-17-03
    in `README.md`). That document's render passes the entry's check whatever token is chosen.
  - **Counter:** this adopts nova-kog #181, which counts `<p class="markdown-alert-title"` (prose can't produce a
    literal `<`).
  - **Tests:** three self-test arms.
    - A, B and C = A + B: A and B at tier 2, C shared and still checked.
    - A small document sharing a long token with a quoting document picks its own tier-1 token.
    - A document under a concatenation keeps the token it shares with a quoting document shared.

    Mutants caught: skipping tier 1, excluding every superset at tier 2, and dropping supersets from the check.

- **Render-log bot updates a queued pull request (#387).** Run 44291452478 found bot pull request #384 already in the
  merge queue, queued by the run before it. GitHub refused the push to its branch (GH006), and the run failed. The
  entry that run rendered then waited for the next docs/log.d merge, because the bot's own merge starts no run: the
  paths filter is the loop guard, and it is unchanged.
  - **The fix:** before pushing to an open bot pull request, `hack/render-log-pr.sh` asks GraphQL whether it is in the
    queue (`isInMergeQueue`). If it is, the script:
    - takes it out with `dequeuePullRequest`;
    - pushes;
    - waits for the required checks on the new commit;
    - enqueues it again.
  - **Queued during the run:** a push refused because the pull request entered the queue after the script looked
    re-reads the state and takes the same path once. A refused push to a pull request that isn't queued fails, and so
    does a queue state that can't be read.
  - **Tests:** `hack/validate-render-log-pr.py` adds three cases: already queued, queued mid-run, and refused while
    not queued. Its local remote refuses pushes to a queued branch the way GitHub does. Six mutants now run inside the
    gate, among them no dequeue and enqueue before the checks pass.

- **Render-log bot waits for its checks and merges from a full history (#379 follow-up).** Two failures on the bot's
  merge-queue path:
  - **Read "no checks yet" as a failure.** Run 44280066151 opened #384 at 09:53:05Z and gave up a second later with
    "failed a required check". `gh pr checks --required --watch --fail-fast` exits non-zero before any check exists on
    a new head.
    - The bot now polls the check runs of exactly the commit it pushed, against the ruleset's own required list.
    - A check not reported yet is waited for, up to 10 minutes to appear and 60 to finish, and then the bot fails
      saying so.
    - A failed required check stops it at once. A non-required check is ignored.
    - A ruleset that requires nothing is refused rather than enqueued unchecked.
  - **Refused to update an open bot pull request.** Run 44281145388 failed with "refusing to merge unrelated
    histories". `render-log.yaml` checked out main at depth 1, so main's commit had no parents.
    - The checkout now uses `fetch-depth: 0`.
    - `render-log-pr.sh` stops if the merge does not start at all, instead of swallowing the error with `|| true`.
  - **Gate:** `hack/validate-render-log-pr.py` gains cases for each state:
    - a check reported late, then passing;
    - a check never reported;
    - a check reported but never finished;
    - a failed check;
    - an empty required set;
    - the update path from an actions/checkout-style clone. At depth 1 it reproduces the refusal; at the workflow's
      depth it merges.

    Six mutants are caught, including the depth-1 workflow and the original "not reported means failed" reading.

- **The docs-rendering screen asks GitHub how admonitions render.** Adopted from
  krateo-blueprints/openstack-nova-operator-kog at ed871ad (#177, #179). There, a `> [!WARNING]` indented in a list
  item rendered as a plain quote, and a `[!NOTE]` swallowed by an open fence rendered as shell. No static check saw
  either.
  - **What it does:** `hack/check-doc-rendering.py` POSTs every markdown file outside `charts/` (63 here) to GitHub's
    `/markdown` (gfm). Each file's count of `markdown-alert-title` must equal all of its admonition markers, fenced
    or not.
  - **Controls:** a known-good fixture must render 1 alert and a known-bad fenced one 0. Otherwise the run is VOID
    (exit 2).
  - **Per-document check:** each response must contain the document's own identifying token. That is its longest
    corpus-unique token outside fences and HTML comments, else its longest shared one, else any block element.
  - **Port:** byte-for-byte for both files (`hack/check-doc-rendering.py` sha256 f5c6e4f5…, `scripts/mdfence.py`
    bd1264c7…).
  - **CI:** it runs in lint.yaml's examples-apiversion job (`hack/gates.d/check-doc-rendering.yaml`), with the job's
    token and `contents: read`.
  - **First run (2026-11-18):** all 63 documents agreed, 0 VOID. No document here declares an admonition yet.
  - **Shared tokens:** 39 documents are identified by a shared token, not a unique one: the 37 files in
    `docs/log.d` plus `docs/log.md`, because `docs/log.md` is their concatenation, and
    `examples/identity-profile/README.md`. That still proves the response wasn't truncated. It doesn't prove the
    response was for this document.
  - **Upstream defect, fixed upstream:** the screen counted `markdown-alert-title` as a substring anywhere in the
    rendered HTML, so a document naming the class in prose counted as one alert. This entry did, and failed. nova-kog
    #181 now counts `<p class="markdown-alert-title"` (prose can't produce a literal `<`), and this entry names the
    class plainly again, as the live test of that counter (#388).

- **Monitoring (#312, phase 1).** New component `openstack-monitoring`, switched by the umbrella's `monitoring` value.
  It is explicit opt-in: only `true` turns it on, and the umbrella refuses `true` where alert-provider's CRDs are not
  served. Unset or false, the render is byte-identical.
  - **What it scrapes.** An OpenTelemetry collector forwards OTLP to the Krateo observability stack. The endpoint is an
    input, and nothing in the defaults is cloud-specific. It scrapes:
    - openstack-exporter 2.0.0-alpha, through its own generated Keystone user;
    - a standalone mysqld-exporter v0.21.0, whose least-privilege MariaDB user the mariadb chart creates without
      restarting the database;
    - RabbitMQ's `/metrics/detailed`.
  - **Why a plain collector Deployment.** It is a Deployment rather than an `OpenTelemetryCollector` CR because the
    Krateo stack runs no OpenTelemetry Operator. Its collectors come from the upstream opentelemetry-collector Helm
    chart.
  - **Alerts.** They are alert-provider CRs in the release namespace, rendered from one source file. Each has a runbook (see
    [the runbooks](runbooks/openstack-api-down.md)) and a scrape-down companion. Each metric name is cited at the
    pinned image's tag.
  - **The gate.** `hack/validate-openstack-monitoring.py` checks the alerts, the credential readers and the switch, and
    plants a defect for each check.
  - **Not yet run on a cluster.**

- **cinder's default refuses; the gates render it as the umbrella delivers it (#308 step 4, #196).**
  - **The flip:** cinder's chart default for `krateo.storageBackend` is now `none`, which refuses to render, instead
    of `unmanaged` (upstream's `rbd1` wiring). Through the umbrella nothing changes, since it always passes
    `storage.backend`. A standalone Cinder Composition must choose: its refusal names the key and the two escapes,
    `unmanaged` and `ceph-external` (README, "Upgrading: a standalone Cinder Composition must choose its storage
    backend"). Diego chose this, keeping `unmanaged` as an explicit opt-in.
  - **Why:** #196 stayed open for a standalone Cinder Composition, which rendered upstream's wiring silently and waited
    in `Init` forever without hand-provided Ceph objects, including `client.admin`.
  - **Gates:** the new `hack/gate_values.py` hands a chart that requires block-storage exactly what
    `storage.backend: ceph-external` adds to its delivered spec, computed from the umbrella's own lines through
    `hack/umbrella_spec.py`; every other chart gets nothing, so its render is unchanged. The 25 gates that render every
    chart (measured: all 25 rendered cinder at its default and failed when the default was flipped without it), the HA
    gate and the image inventory pass it; the inventory is unchanged. A renderer that misses it renders cinder's refusal
    and fails. `validate-storage-backend` check 3 and the cinder
    divergence entry assert the new default.

- **A disruption budget for the RabbitmqCluster (#309, H2).** The RabbitMQ cluster-operator renders no
  PodDisruptionBudget, so a node drain could evict two of three brokers and take the quorum queues' majority. In
  operator mode with more than one replica, the rabbitmq chart now renders one: `maxUnavailable: 1` on the cluster's
  pods. A single broker gets none, because there a budget would only block drains. `hack/validate-messaging-operator.py`
  checks both.

- **RabbitMQ users and vhosts as topology-operator resources (#309, H2b).** With `messaging.backend: operator`,
  helm-toolkit renders each component's rabbit-init as messaging-topology-operator resources instead of a Job:
  - **User:** imports `<su>-rabbitmq-user`, an ExternalSecret projecting the user's generated root. The operator
    re-syncs the password when the root rotates.
  - **Vhost:** quorum by default, retained when its resource is deleted.
  - **Permissions:** one for the user and one for the administrator, which ceilometer needs to read other components'
    vhosts.
  - **Policies:** one per chart policy, without the `ha-*` keys.
  - **Waits:** every pod that waited on `<su>-rabbit-init` waits on those resources being Ready, through
    kubernetes-entrypoint's condition waits, including the waits only the init container adds
    (`dependencies.dynamic.job_rabbit_init`). Each wait is granted to the pod's ServiceAccount (#370).
  - **Charts that ship rabbit-init off:** render nothing and wait on nothing.
  - **Chart mode** is unchanged.
  - **New gate:** `hack/validate-messaging-topology.py`.

- **The pull-request gates measure the pull request's own change.** `hack/validate-log-untouched.py` diffed
  `BASE..HEAD` on a pull request, with BASE the pull request's `base.sha`. That can be older than main's tip, so any
  bot render of `docs/log.md` that landed after the branch forked counted as the branch's change. #376 at 8d5240e
  failed this way without touching the file. `hack/validate-no-skip-markers.py` read commits over the same range, so
  the skip markers in the old bot renders' messages would have failed a stale branch too.
  - **Fix:** on a pull request, both gates now measure from the first parent of the merge ref that actions/checkout
    checks out (`refs/pull/<n>/merge`, main's tip as GitHub merged it). When HEAD isn't a merge commit, they measure
    from the merge base of BASE and HEAD instead. The merge base alone isn't enough on the merge ref: there it is
    BASE itself, and main's later commits still count.
  - **Unchanged:** a merge group's range (`merge_group.base_sha` is fixed, and the group's commits sit on top of it)
    and a push's range (the previous head) were already right.
  - **Tests:** each gate now builds throwaway repositories in which main gains a commit after the fork:
    - log-untouched: a branch that only adds a log.d entry passes, and one that edits `docs/log.md` still fails, both
      on the merge ref and at the head;
    - skip-markers: a marker in main's commit isn't read, and the branch's own is.

    Mutants caught: two-dot restored in either gate, and the merge base alone in log-untouched.

- **Every reusable workflow pinned (krateo-blueprints/charts rule 9, charts#20).** The three krateo-platformops/.github
  calls are pinned to `f25d6b5 # main 2026-11-17`: `lint-docs.yaml`, `release-notes.yaml` and `security.yml`. That
  repository has no tags yet, and `security.yml` runs here with `security-events: write`. `lint.yaml` gains a
  `pinned-uses` job, the shared check from charts 1.2.0 (`33c7951`). It refuses any non-local reusable-workflow `uses:`
  that is not a 40-hex commit with a `# X.Y.Z` or `# main YYYY-MM-DD` comment. `release-tag.yaml` keeps
  `788f1a6 # 1.1.0`, since the publisher and the preflight are byte-identical at 1.2.0. Run here, the check reports
  6 reusable uses, 0 not pinned. `pinned-uses / pinned-uses` becomes a required merge-queue check in
  `hack/merge-queue-ruleset.json`, and `hack/validate-merge-queue-ruleset.py` maps its inner job name. The live ruleset
  is updated after merge.

- **The render-log bot enqueues through enqueuePullRequest.** The merge-queue ruleset is applied (2026-11-18), and the
  bot's queue step would have failed on its next render. It ran `gh pr merge --auto --squash`, and gh (2.86) does
  that through `enablePullRequestAutoMerge`, as it does `gh pr merge` without `--auto`. This repository has
  auto-merge disabled, so the call fails with "Auto merge is not allowed for this repository". The queue is the
  mechanism, so auto-merge stays off.
  - **Change:** under a merge queue, `hack/render-log-pr.sh` now waits for its pull request's required checks
    (`gh pr checks --required --watch`). It then adds the pull request to the queue with the GraphQL
    `enqueuePullRequest` mutation, using the pull request's node id and the App token. It never runs `gh pr merge`.
    If a required check fails, nothing is enqueued.
  - **Tests:** `hack/validate-render-log-pr.py` case 6 now requires the checks wait, then one enqueue mutation with
    the node id, and no `gh pr merge`. A new case 7 covers a failed check: nothing is enqueued or merged. A mutant
    that brings back `--auto` is caught.

- **RabbitMQ cluster-operator mode for fresh installs (#309, H2b).** `messaging.backend: operator`, opt-in, with
  `chart` the default.
  - **The broker:** a RabbitmqCluster `openstack-mq`, run by the cluster-operator from
    krateo-blueprints/rabbitmq-operator-blueprint. The image is pinned to `rabbitmq:4.3.4-management`, the replicas come
    from the one existing source, and persistence uses the default StorageClass unless set.
  - **Credentials:** the administrator comes from the generated root, through `secretBackend.externalSecret`. The
    rabbitmq chart renders nothing of chart mode.
  - **Refusals:** the chart refuses a cluster that doesn't serve `rabbitmqclusters.rabbitmq.com`, and an install
    already running the chart-mode broker; migrating one is H2c.
  - **Clients:** every component that lists RabbitMQ hosts lists the cluster's pods
    (`openstack-mq-server-N.openstack-mq-nodes`); watcher lists the headless Service.
  - **Quorum queues:** every component that builds a transport URL gets them (`rabbit_quorum_queue`,
    `rabbit_transient_quorum_queue`, `use_queue_manager`, `rabbit_stream_fanout`, prefetch 1, `rabbit_ha_queues`
    false), because RabbitMQ 4.x refuses the classic transient queues. Their rabbit-init policies lose the `ha-*`
    keys 4.x refuses and keep `message-ttl`.
  - **Overrides refused:** a per-component `rabbit_ha_queues: true` or statefulset override.
  - **Chart mode:** every render is unchanged.
  - **New gate:** `hack/validate-messaging-operator.py`.

- **Custom-resource waits may read their resource (#309).** helm-toolkit's `kubernetes_pod_rbac_serviceaccount` now
  renders, for a component whose dependencies list `custom_resources`, a Role granting `get` on exactly each named
  object (its group, its `resource` plural, `resourceNames`) and a RoleBinding to the pod's ServiceAccount.
  kubernetes-entrypoint (pinned `c606628c`) already waits on custom resources by `status.conditions`, but the dependency
  Role covered only core, batch, apps and discovery resources, so every such wait would have been Forbidden. An entry
  without `resource`, or with a core apiVersion, is refused at render time. No chart declares `custom_resources` yet:
  every render is unchanged. It unblocks the operator CRs that replace db-init and rabbit-init (#309, H2b/H3b). New gate:
  `hack/validate-custom-resource-wait-rbac.py`.

- **Upstream divergences, one file per entry.** Every change that diverged from upstream appended to
  `hack/upstream-divergences.yaml`, so each merge re-conflicted the branches in flight (#352, #353, #355, #344 and
  #357 on 2026-11-18). Each divergence is now `hack/upstream-divergences.d/<component>-<slug>.yaml`, holding one
  entry. The 19 entries were split out with their text and the comments above them, including #344's three. Loaded
  from the directory, they are the same 19 entries and the same 81 assertions as the old file at 9c177be. The old file is removed.
  - **Refusals:** `hack/validate-upstream-divergences.py` reads the directory and refuses, before checking anything:
    - a file that isn't exactly one mapping of `component`, `issue`, `reason` and `assertions` (two entries, a list,
      or a second YAML document);
    - a duplicate key;
    - a file whose name doesn't start with its component;
    - an assertion already declared in another file;
    - an empty directory;
    - the old file coming back.
  - [release.md](release.md) says how to add one.
  - The case lists of `hack/validate-removed-values.sh` stay as they are for now.

- **Publisher 1.1.0 and the release preflight.** `release-tag.yaml` pins krateo-blueprints/charts 1.1.0 (`788f1a6`).
  - **Publisher:** from 1.1.0 it refuses any version that is not plain `X.Y.Z` (its rule 13).
  - **Preflight (rule 12):** a new `preflight` job calls the `release-preflight.yaml` reusable, and `package-and-push`
    `needs:` it, so a refusal publishes nothing.
  - **The hook:** `hack/release-preflight` refuses a tag whose commit has no `X.Y.Z-main.g<sha7>` pre-release in GHCR.
    It also refuses when GHCR cannot be asked.
  - **Why the hook:** nothing else stopped a tag on a commit that skipped the pre-release channel, or a tag on a
    branch commit. Lint runs on every pull request, so a branch commit passed the lint check too.
  - **What it cannot prove:** that the GKE pair passed.
  - **Gate:** `hack/validate-prerelease-channel.py` checks the pins, the `needs:` chain and the hook's behaviour
    against a stubbed GHCR. All 45 mutations are caught. See [release.md](release.md).

- **render-log.py refuses arguments it does not know.**
  - **What happened:** `hack/render-log.py` ignored every `--` argument it did not recognise and fell through to write
    mode. So `render-log.py --help`, or a typo such as `--chek`, rewrote `docs/log.md` in the working tree, a file a
    pull request must never carry (`hack/validate-log-untouched.py`). `-h` was taken as the repository root.
  - **The fix:** arguments are now parsed with argparse before anything is read or written. Only the root and
    `--check` or `--check-rendered` (one of them) are accepted. Unknown flags, two roots and both modes at once are
    refused, and so is a root that is not an existing directory holding `docs/log.d`. `--help` prints the usage.
  - **The new gate:** `hack/validate-render-log-args.py` runs the renderer in a throwaway copy and checks:
    - each refusal exits non-zero with its reason and leaves `docs/log.md` byte-identical;
    - `--help` and `-h` only print the usage;
    - as positive controls, write mode, both `--check` orders and `--check-rendered` still work.

    Against the previous renderer it fails on the unknown-flag, two-roots, both-modes and `--help` cases, which
    rewrote `docs/log.md`, and on `-h`.

- **Merge queue on main, drafted.** Diego approved GitHub's merge queue (2026-11-18), so every commit that lands is
  tested as it will exist on main. Until now each pull request was brought up to date with main and waited for, one
  at a time. Nothing is applied to the repository settings yet.
  - **lint.yaml** runs on `merge_group`. The job-hooks base comes from `merge_group.base_sha`.
    `hack/validate-log-untouched.py` and `hack/validate-no-skip-markers.py` read a merge group commit by commit, as
    a push. Each queued pull request is its squash commit there, so the message that would land is checked.
  - **The ruleset draft**, `hack/merge-queue-ruleset.json`, adds a merge queue (squash, ALLGREEN, groups of up to 5,
    a 60-minute check timeout) and requires the 12 checks lint.yaml reports, from GitHub Actions. The informational
    `lint` job is left out. `hack/validate-merge-queue-ruleset.py` fails when the two drift apart.
  - **Not gating:** image-scan (it scans every image, not the changed ones) and the security workflow.
  - **Releases:** `hack/require-lint-passed.sh`, extracted from release-tag.yaml, accepts a successful lint run on
    the exact commit from its merge group, its push to main, or a dispatch. A pull request's run is no longer
    counted, and each run's `head_sha` is re-checked. `hack/validate-require-lint-passed.py` covers 8 cases.
  - **The render-log bot** enables auto-merge when main has a merge queue, instead of merging (a new case in
    `hack/validate-render-log-pr.py`). A queue entry made with `GITHUB_TOKEN` would start no `merge_group` run, so
    the App token (#356) comes first, then the ruleset.
  - [release.md](release.md) has "How a pull request merges".

- **The render-log bot acts as a GitHub App; no skip markers.** The first bot render landed (d1cb62b) with two
  defects, measured 2026-11-18:
  - **Skipped workflows.** #347's squash message quoted the bot's skip marker, so GitHub skipped every push workflow
    on main for that commit, lint included.
  - **Unlinted bot commits.** The bot merged with `GITHUB_TOKEN`, and GitHub starts no workflow for a `GITHUB_TOKEN`
    event, so its render commits on main got no lint run. prerelease and release-tag require a green lint on the
    exact commit.

  Diego chose a GitHub App:
  - `render-log.yaml` mints the App's token with `actions/create-github-app-token`, pinned to a commit. It refuses
    without `RENDER_LOG_APP_CLIENT_ID` and `RENDER_LOG_APP_PRIVATE_KEY`, and gives `GITHUB_TOKEN` no write
    permission.
  - `hack/render-log-pr.sh` refuses an App whose slug isn't `hack/render-log-app.yaml`'s, commits as
    `<slug>[bot]`, and drops the skip marker. The loop guard is the paths filter alone: the bot's commit changes
    `docs/log.md`, which the filter leaves out.
  - `hack/validate-log-untouched.py` now exempts only `krateo-openstack-render-log[bot]` on a `bot/render-log-*`
    branch. The retired `github-actions[bot]` login is refused.
  - New gate `hack/validate-no-skip-markers.py` refuses any skip marker GitHub honours in a pull request's title,
    body or commit messages. That covers the bracketed "skip ci" family and the `skip-checks: true` trailer, in any
    case, quoted or not.
  - `hack/validate-render-log-pr.py` adds two cases. One reads the workflow: the paths leave `docs/log.md` out, the
    action is pinned, the job refuses without the App's secrets, and nothing has write permission. The other checks
    that another App's token is refused.
  - [release.md](release.md) lists the App's permissions and the steps to create it.

- **mariadb's remote backup and its ks-user Job removed (#302).** Turning on `manifests.job_ks_user` deadlocked a
  first install. The Job creates the Keystone user that Swift/RGW remote backups authenticate as. It is a
  post-install hook that waits on keystone (#301), and keystone waits on mariadb. The user it created also had role
  admin and OpenStack-Helm's published password. Diego decided (2026-11-17) to remove the path rather than move it.
  - **Removed:** the ks-user Job and script, the RGW Secret, `endpoints.identity` with `mariadb-backup-user`,
    `conf.backup.remote_backup`, and the backup CronJob's remote environment and Keystone credentials. The CronJob's
    wait on `mariadb-ks-user` is gone too.
  - **Refused:** `manifests.job_ks_user: true` and any `conf.backup.remote_backup` other than `enabled: false`, by the
    schema and by `templates/removed-values.yaml` when the schema is skipped. `false` stays accepted for older
    Compositions.
  - **Local backups are unaffected.** With `manifests.cron_job_mariadb_backup` and `conf.backup.enabled` on, the
    CronJob renders with `REMOTE_BACKUP_ENABLED=false` and waits only on mariadb.
  - **Gates:**
    - `hack/validate-removed-values.sh` gains nine mariadb cases.
    - `hack/validate-readiness-deadlock.py` now also renders every chart with every `manifests.job_*` switch on. It
      fails on a cycle that appears only with an opt-in Job on, which is how the ks-user Job got past it.
    - An upstream-divergences entry keeps the script, the user and the remote settings out across a re-vendor.

- **Uninstall deletes a component's hook Jobs (#206).** Since 0.3.41 every Job is a `post-install,post-upgrade` hook
  (#179), and Helm never deletes hook resources at uninstall: cinder's removal left six completed Jobs and their pods,
  and a full teardown 156 pods (#204). `hook-succeeded` can't be used, because kubernetes-entrypoint waits on dependency
  Jobs by name while the component is installed. Each of the 32 charts that render hook Jobs now includes
  helm-toolkit's `job_hook_cleanup`. It is a `pre-delete` hook, weight 100, so it runs after db-drop and the clean
  Jobs. It selects `release_group=<release>`, leaves out itself, and deletes the Jobs whose `helm.sh/hook` names
  post-install or post-upgrade, with background propagation so their pods go too. Its ServiceAccount, Role (list and
  delete on Jobs, nothing else) and RoleBinding are plain release objects, so they exist while it runs. It uses the
  chart's own ks-user (or db-init, or rabbit-init) image, which has python, and always exits 0, so a failed cleanup
  leaves Jobs behind rather than blocking the uninstall. That pre-delete hooks run under the controller was measured in
  #204 (glance-clean ran at teardown); this hook itself hasn't been run on a cluster yet. New gate
  `hack/validate-hook-cleanup.py` renders each chart at defaults and with every Job on. For every render with a hook
  Job, it requires exactly one cleanup, last among pre-delete hooks, with that RBAC and a selector matching every hook
  Job but not itself. It also runs the script against a stand-in API, with another release's Job and a non-hook Job
  next to the render's, and against a failing API. Its first CI run found four hook Jobs with no labels at all
  (cloudkitty-storage-init, ironic-manage-cleaning-network, magnum-domain-ks-user, masakari-db-sync), which no
  selector can reach. They now carry helm-toolkit's labels; their pod templates are unchanged. libvirt, memcached, openvswitch and redis render no hook Job unless
  `images.local_registry.active` turns on image-repo-sync, and have no python image, so they get no cleanup.

- **trove's db-purge CronJob removed (#289).** It rendered by default and ran `trove-manage db_purge --age_in_days 30`.
  trove-manage on stable/2026.1 has no `db_purge`: `trove/cmd/manage.py` registers 12 commands, none a purge, and no
  file in trove's tree defines a database purge. Every scheduled run would have failed at argument parsing, and a
  failing CronJob, unlike a failing hook, blocks nothing that anyone sees. With no command to point it at, the CronJob,
  its script and values are gone. `manifests.cron_job_db_purge: true` is refused by the schema and by
  `templates/removed-values.yaml`; `false` is still accepted for older Compositions (`hack/validate-removed-values.sh`
  covers both). New gate `hack/validate-cron-commands.py`: every `<project>-*` program that a CronJob's script starts,
  in every chart and whether or not the CronJob is on by default, must be given a command listed for it in
  `hack/cron-program-commands.yaml`, measured from the program's stable/2026.1 source. It is static, since the command
  is in the script text. It found no other CronJob running a missing command (15 CronJobs, 20 commands).

- **ceilometer publishes only to gnocchi (#294).** Both pipelines listed `notifier://` next to `gnocchi://`. The
  notifier publisher sends every sample and event back onto ceilometer's vhost, as topics `metering` and `event`
  (stable/2026.1 `ceilometer/publisher/messaging.py`), and nothing in this blueprint listens there: their consumers, the
  collector and panko, are retired. On GKE (2026-11-17) `event.sample` went from 954 to 1062 messages in 10 minutes
  with 0 consumers, and `metering.sample` grew too. The sinks now publish to `gnocchi://` alone. New gate
  `hack/validate-ceilometer-publishers.py` renders ceilometer and requires each publisher to reach a consumer in its
  `deps` closure: gnocchi's API, or a listener on that topic and vhost other than ceilometer's own agent (which would
  re-ingest its own samples). Any other publisher fails until its consumer is measured. An upstream-divergences entry
  keeps `notifier://` out across a re-vendor. On an existing install the two queues stop growing but keep what they
  hold until deleted (`rabbitmqctl delete_queue -p ceilometer event.sample`, and `metering.sample`). ceilometer.conf
  sets no `[oslo_messaging_rabbit]`, so at oslo.messaging's defaults they're non-durable and a RabbitMQ restart also
  drops them (read from the defaults, not measured live).

- **High availability, first phases (#309), and nova-compute liveness (#146).**
  - **`ha.enabled` (opt-in):** memcached as 3 replicas behind a headless Service, every component listing each
    replica, and a refusal while fewer than 3 nodes carry `openstack-control-plane=enabled`. More than one memcached
    replica without `ha` is refused. Off renders as before.
  - **RabbitMQ's host count** comes from `componentValues.rabbitmq.pod.replicas.server` alone and reaches every
    transport URL and ceilometer's notification URLs; a conflicting per-component count is refused.
  - **nova-compute** liveness and startup check the process only; readiness keeps the RPC check.
  - **New gates:** `hack/validate-ha-propagation.py` and `hack/validate-nova-compute-liveness.py`.

- **image-scan retries a scan cut short by a layer download (#316, #334).**
  - **What failed:** image-scan run 44202269356 (on `feat/309-h0-h1`) failed in
    `scan (quay.io/openstack.kolla/gnocchi-metricd:2026.1-ubuntu-noble)`. The cause was no new CVE: Trivy stopped
    with `failed to analyze layer ... unable to read the file: unexpected EOF`, a layer download cut short. The gate
    then failed, correctly, on the image's missing report.
  - **Measured on 2026-11-18:** the waived series tag still serves the recorded digest (`sha256:12c53f1f…`), so it
    had not moved upstream. Main's image-scan on `08ec684`, a few minutes later, scanned the same digest with no
    finding outside the allowlist.
  - **The fix:** the scan step now tries up to three times and drops only the layer-analysis cache between attempts.
    A real finding can't be retried away, because findings are judged from the report. An image that fails all
    three attempts still has no report, and the gate fails it by name.
  - **The allowlist doesn't change:** there was no new finding, and entries are only made for measured findings.

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

- **Public endpoints over HTTPS through the Gateway API (#311, phase 1).**
  - **Umbrella `publicEndpoints`:** off by default, and off renders byte-identical. When on, the umbrella renders:
    - one Gateway (`gatewayClassName`, default `agentgateway`, which Krateo's `features.ingress` installs);
    - an HTTPRoute per public endpoint of every selected component;
    - an HTTP-to-HTTPS redirect;
    - a cert-manager Certificate from `tls.issuerRef`;
    - an ingress NetworkPolicy set: deny by default, allow the namespace, `nodeCIDRs`, the Gateway proxy, and
      `allowNamespaces`. The policies select every pod without the label `krateo.io/networkpolicy-exempt`, in block
      style; the label is an explicit opt-out.
  - **The components:**
    - Each public catalog URL becomes `https://<service>.<baseDomain>`, with proxy-header parsing set.
    - The charts' inert Ingress switches go off (upstream removed those templates in 6ec1132485).
    - Horizon becomes ClusterIP with Secure cookies.
    - Routes set HSTS, and overwrite X-Forwarded-Proto with `https`.
    - Never routed: metadata, RabbitMQ, MariaDB, memcached.
  - **Refused on a live cluster** without the Gateway API or cert-manager CRDs, and on a missing input.
  - **The new gate:** `hack/validate-public-endpoints.py` checks:
    - off equals before;
    - the routes against what each chart registers;
    - the deny list, HSTS and redirect, and the policy's exact peers;
    - the refusals;
    - every routed chart rendered with the values the umbrella gives it (backend Service, catalog URL, no Ingress,
      proxy headers, Horizon);
    - the tables against the charts.

    It also runs thirteen mutations, each of which must be reported as the defect it plants.
  - **Not yet verified on a cluster.** The live checks are in the README. Follow-ups: #325, #326, #327.

- **Consume a Ceph cluster without client.admin (#308 phase P1, steps 1-3; rendered and gated, not yet run live).**
  - **Copied keys:** a third credential kind, `source: provided`. With `storage.backend` ceph-rook or ceph-external, the
    umbrella copies each Ceph user's key and `mon_host` from where Ceph keeps them (Rook's `rook-ceph-client-<name>`
    and `rook-ceph-config`, or the operator-named `storage.ceph.keys`) through a SecretStore limited to exactly those
    Secrets. Copies refresh every 5 minutes and are not kept past the release.
  - **Charts:** cinder, libvirt and (with `storage.ceph.glanceStore: rbd`) glance project the copies. No client.admin
    keyring is rendered, no `ceph auth` runs and no pool is created: storage-init checks the pool with the user's own
    key. Each reader carries a live checksum of its key, so it restarts when Ceph's side rotates it. Without a Ceph
    backend every chart renders as before.
  - **One source:** pools, users and the libvirt secret UUID reach cinder, glance, libvirt and nova from
    `storage.ceph` only; `componentValues` may not set them, nor wire Ceph by hand.
  - **Held, refused, never stuck:** a Ceph consumer is created only once the copies of its keys report Ready; the
    Openstack Composition refuses while ESO reports the copy store unable to read the source Secrets, naming the grant;
    glance refuses to move its store to `rbd` off a live `glance-images` claim until `storage.ceph.glanceImagesMigrated`.
  - **Gates:** the new `hack/validate-ceph-consumer.py` (27 copied roots, P projections, 22c reader closure against
    the measured `hack/ceph-readers.yaml`, 18c readers restart, 18d the checksum can read its Secret, 28 no
    client.admin, 29 delivery and consistency through `hack/umbrella_spec.py`, 30 the hold and the read-grant refusal);
    `hack/validate-glance-store.py` adds the migration refusal on the same single claim lookup. `validate-pod-references` renders the storage-dependent charts as the umbrella
    delivers them.

- **cinder refuses to render without a block-storage backend (#196, phase P0 of #308).**
  - **What was wrong:** cinder's only backend is Ceph RBD. With no Ceph, `cinder-volume` and `cinder-storage-init`
    waited in `Init` forever on `ceph-etc` and keyring Secrets nothing creates. That was the state on every default
    install (#179, #204).
  - **Now:** the Openstack Composition has `storage.backend`, `none` by default, typed by the generated schema. The
    umbrella passes it to every component that requires block-storage (cinder) as `krateo.storageBackend`, and refuses a
    `componentValues` override. With `none`, cinder fails its render and names the value. The Ceph backends
    (`ceph-rook`, `ceph-external`) are accepted by the schema and refused by cinder until #308 implements them.
  - **Selection:** `requiresProvisioning` became `components[].requires`, a capability list (`block-storage`,
    `pxe-interface`, `lb-mgmt-net`, `rwx-storage-class`). `full-strict` selects a component only when the install
    provides everything it requires; only `block-storage` can be provided, by `storage.backend`. `full` is unchanged.
  - **Gates:**
    - `hack/validate-provisioning-preconditions.py` also renders `full-strict` with every non-`none` backend. That is
      the positive control: it must add cinder and nothing else.
    - The new `hack/validate-storage-backend.py` checks that the value reaches cinder alone, that the umbrella is the
      one place that sets it, and that cinder refuses without a backend.
    - `hack/validate-pod-references.py` renders cinder once per backend the umbrella can deliver. Its #196 allowlist
      entries are gone.
  - **Also:** nova-compute's `LIBVIRT_CEPH_SECRET_UUID` read the undefined `conf.ceph.secret_uuid` (an upstream defect),
    so it rendered with no value. It now reads `conf.ceph.cinder.secret_uuid`, and the new gate renders it with a
    sentinel.

- **docs/log.md is rendered on main, by a bot pull request.** After #340, every branch still committed a regenerated
  `docs/log.md`, so merges kept conflicting on it (#322, #320).
  - **Pull requests:** a pull request now adds its `docs/log.d` entry and nothing else.
    `hack/validate-log-untouched.py` fails one whose diff touches `docs/log.md`. It makes one exception, for the
    bot's own pull request, and only when the branch is `bot/render-log-*` and the author is `github-actions[bot]`.
    The rule is checked case by case first (prefix with a human author, bot author on another branch, a look-alike
    prefix, another bot). `hack/render-log.py --check` now validates only the entries: names with real dates, front
    matter, the bullet, links that resolve from `docs/log.d`, and no two entries with the same date and number.
    CI checks the pull request merged with current main, so a number an entry merged ahead already took fails, and
    the author takes the next free one.
  - **Main:** after each merge, `.github/workflows/render-log.yaml` re-renders `docs/log.md`. If it changed,
    `hack/render-log-pr.sh` commits it as `github-actions[bot]` on `bot/render-log-<sha7>` with `[skip ci]`, opens a
    pull request and squash-merges it. If a bot pull request is already open, it updates that one with a
    fast-forward instead. It runs one at a time and commits nothing when the render is unchanged. It's a pull request
    rather than a push because main's org ruleset requires one and has no bypass actor (Diego, 2026-11-18). It needs
    "Allow GitHub Actions to create and approve pull requests", which is off for the org and the repo (checked
    2026-11-18).
  - **Gate:** `hack/validate-render-log-pr.py` runs the script against a local remote and a stand-in `gh`, in four
    cases: current, a new entry, a bot pull request already open, and after landing.

- **neutron's tunnel interface is detected, not assumed.** `network.interface.tunnel` defaulted to `ens4`, a GCE NIC
  name. On a node without `ens4` the OVS agent's init set no tunnel IP ("Var LOCAL_IP is empty"). The default is now
  empty, so the init uses the interface of the default route. That logic was already in the script; the `ens4` default
  had been skipping it.
  - **Existing installs keep `ens4`.** The old schema default was written into each stored neutron object (#179). It is
    harmless on GCE, where `ens4` is the right NIC. Elsewhere, clear `componentValues.neutron.network.interface.tunnel`
    or set it to the node's NIC.
  - **New gate:** `hack/validate-neutron-tunnel-interface.py` checks the empty default and the schema, and runs the
    rendered fallback.

- **Images pinned; remediation issues (#316, #328 to #334).**
  - **Dated tags:** Diego decided on 2026-11-17 to pin dated tags and verify their digests in CI. The 31 airshipit
    release-series images now use `2026.1-ubuntu_noble-20261117`, and `docker.io/rabbitmq:3.13-management` became
    `3.13.7-management`. Each new tag serves the digest that was recorded and scanned, so no bytes change. It is also
    the newest dated build, so no baseline CVE is fixed and the allowlist keeps all 167 entries, re-keyed.
  - **libvirt:** appVersion was re-measured on its dated image: libvirt 10.0.0, from Trivy's package list.
  - **Gates:**
    - `hack/gen-image-inventory.py` fails on a release-series tag unless `hack/image-pin-waivers.yaml` waives it, and
      on a stale or expired waiver.
    - With `--resolve` (image-scan), it fails when a pinned tag serves a digest other than the recorded one, and
      `--write` keeps the recorded digest.
    - `validate-app-version.py` and `validate-keystone-oidc-image-series.py` accept the dated suffix and compare the
      series.
  - **Mirror:** `hack/image-mirror.yaml` and `.github/workflows/image-mirror.yaml` copy the 4 Kolla gnocchi images by
    digest to dated tags in `ghcr.io/krateo-blueprints/images/`. The copy runs on main or by hand. It never moves a
    tag, and it fails until the package pulls anonymously. The 4 images are waived until 2027-01-31 (#334).
  - **Remediation issues:** #328 (PyJWT, from `stable/2026.1` upper-constraints), #329 (nginx 1.13.3 in ironic),
    #330 (drop the rally test pods), #331 (docker 27.5.0 and image_repo_sync), #332 (PowerDNS), #333 (memcached) and
    #334 (Kolla gnocchi). Candidate replacements were scanned, so each issue states the CRITICAL count the fix leaves.

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
