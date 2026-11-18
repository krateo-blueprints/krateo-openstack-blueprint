---
type: Log
title: High availability, first phases, and nova-compute liveness
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **High availability, first phases (#309), and nova-compute liveness (#146).**
  - **`ha.enabled` (opt-in):** memcached as 3 replicas behind a headless Service, every component listing each
    replica, and a refusal while fewer than 3 nodes carry `openstack-control-plane=enabled`. More than one memcached
    replica without `ha` is refused. Off renders as before.
  - **RabbitMQ's host count** comes from `componentValues.rabbitmq.pod.replicas.server` alone and reaches every
    transport URL and ceilometer's notification URLs; a conflicting per-component count is refused.
  - **nova-compute** liveness and startup check the process only; readiness keeps the RPC check.
  - **New gates:** `hack/validate-ha-propagation.py` and `hack/validate-nova-compute-liveness.py`.
