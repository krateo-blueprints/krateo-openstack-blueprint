---
type: Runbook
title: "Runbook: a Nova service or Neutron agent is down"
description: What to check when openstack-nova-service-down or openstack-neutron-agent-down fires for a host and binary.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-monitoring
tags: [openstack, monitoring, runbook, nova, neutron]
timestamp: 2026-11-17T00:00:00Z
---

# A Nova service or Neutron agent is down

**Alerts:** `openstack-nova-service-down`, `openstack-neutron-agent-down` (namespace `krateo-system`), grouped by
`hostname` and `service`.

**What fired:** `openstack_nova_agent_state` (Nova reports the service down) or `openstack_neutron_agent_state` (the
agent is not alive) stayed below 1 for two consecutive 5-minute windows, for the host and binary the group names.
Both report through RabbitMQ heartbeats, so a service that runs but cannot reach RabbitMQ reads down too.

## Check, in this order

1. **Which pod:** the group's `hostname` is the node (computes, agents) or the pod name (conductor, scheduler). Find it
   with `kubectl -n openstack get pods -o wide`.
2. **Its logs and restarts.** A crash-looping worker without a readiness probe can read Ready in Kubernetes (#231), so
   trust the restarts and the logs, not the Ready column.
3. **RabbitMQ:** the pod's connection errors, and `openstack-rabbitmq-queue-backlog` for its queues.
4. **For the OVS agent:** the `openvswitch` pod on the same node.

## Do not

- Delete the service record (`openstack compute service delete`) while the pod still exists: it re-registers, and the
  record's history is lost.
- Read a resolved state from HyperDX for a group that stopped reporting: HyperDX never resolves it. Read the Alert's
  `status.groups`.
