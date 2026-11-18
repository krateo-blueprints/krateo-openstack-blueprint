---
type: Log
title: A disruption budget for the RabbitmqCluster
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **A disruption budget for the RabbitmqCluster (#309, H2).** The RabbitMQ cluster-operator renders no
  PodDisruptionBudget, so a node drain could evict two of three brokers and take the quorum queues' majority. In
  operator mode with more than one replica, the rabbitmq chart now renders one: `maxUnavailable: 1` on the cluster's
  pods. A single broker gets none, because there a budget would only block drains. `hack/validate-messaging-operator.py`
  checks both.
