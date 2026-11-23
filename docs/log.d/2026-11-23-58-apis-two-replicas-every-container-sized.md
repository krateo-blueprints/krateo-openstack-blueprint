---
type: Log
title: APIs keep running through a rollout; every container is sized
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
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
