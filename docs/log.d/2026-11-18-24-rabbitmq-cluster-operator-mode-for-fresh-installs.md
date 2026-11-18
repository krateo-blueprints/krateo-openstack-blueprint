---
type: Log
title: RabbitMQ cluster-operator mode for fresh installs
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
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
