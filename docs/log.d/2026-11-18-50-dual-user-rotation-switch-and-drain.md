---
type: Log
title: A RabbitMQ user rotation adds the new user, switches once it is ready, and retires the old one once every reader has rolled
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
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
