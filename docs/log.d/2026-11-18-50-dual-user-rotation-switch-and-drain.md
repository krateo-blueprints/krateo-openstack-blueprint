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
  - **The evidence:** CDC writes no observedGeneration on a Composition (`internal/composition/support.go`
    setStatus, and unstructured-runtime v1.1.0's conditions set none). So the evidence that the switching release
    was applied is each Deployment, StatefulSet, DaemonSet and CronJob carrying the new generation.
  - **Rollout:** then kubectl v1.36.4's rollout predicates (`rollout_status.go` :75-87, :107-112, :130-146), with a
    partition counted as not rolled while any pod is below it. OnDelete, bare Pods and unreadable objects are kept
    and reported, never forced.
  - **Jobs:** no Job running, CronJobs included.
  - **Hook Jobs** aren't in `status.managed`, but each is bounded by `activeDeadlineSeconds`. So the previous user
    stays until the service's projections of the new root have synced (ESO `syncedResourceVersion` at their own
    generation) and that deadline has passed since (1800 s by default).
- **Entrypoint waits:**
  - **Own service:** a pod waits on its own service's **active** User and Permission. Without this, a pod restarting
    after the generation-0 user's deletion would have waited forever.
  - **Another service:** a pod waits only on that service's vhost and the administrator's permission (ceilometer's
    notification agent waited on four services' Users). The umbrella passes `operator.serviceUser` for this, and at
    0.3.74 ceilometer-notification rolls once for it.
- **The live read:** a rotation reads the live component with its own GET (`osh.liveMessaging`), never the hold's
  read (`validate-two-phase-values`, which failed #422's first run).
- **Drift:** the latch also covers generation roots, by Secret name.
- **Guard:** `validate-messaging-rotation` (capability-roles 492):
  - 33 step cases with the lookups stood in, from heat's own `status.managed`. They cover each step, the
    second-token wait, the deletion of the old User, and half-finished rollouts for every kind and failure.
  - 7 umbrella render checks: the generation projection, the report, the span with and without a rotation, and drift.
  - All 20 rotating charts: one user per generation, waits on the active one only, every reader rolls, no non-hook
    Job changes, the readers are exactly `osh.messagingReaders`, and the hook deadlines are bounded.
  - 6 mutants killed.
