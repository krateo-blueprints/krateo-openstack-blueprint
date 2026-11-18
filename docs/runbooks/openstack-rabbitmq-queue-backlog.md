---
type: Runbook
title: "Runbook: a RabbitMQ queue is not being drained"
description: What to check when openstack-rabbitmq-queue-backlog fires for a vhost and queue.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-monitoring
tags: [openstack, monitoring, runbook, rabbitmq]
timestamp: 2026-11-17T00:00:00Z
---

# A RabbitMQ queue is not being drained

**Alert:** `openstack-rabbitmq-queue-backlog` (in the OpenStack release namespace), grouped by `vhost` and `queue`.

**What fired:** `rabbitmq_detailed_queue_messages` (ready plus unacknowledged) stayed above 1000 for a whole 30-minute
window: its minimum over the window was above the threshold, so the queue is not consumed, or not fast enough.

## Check, in this order

1. **Its consumers:** `kubectl -n openstack exec rabbitmq-rabbitmq-0 -- rabbitmqctl list_queues -p <vhost> name messages consumers`.
2. **No consumer:** find which service should consume it. A queue whose consumer was retired upstream grows forever:
   ceilometer's `event.sample` and `metering.sample` are one known case (#294).
3. **Consumers present but slow:** the consuming service's logs and its worker count.

## Do not

- Purge the queue to clear the alert before knowing what it holds: RPC requests and notifications are lost with it.
