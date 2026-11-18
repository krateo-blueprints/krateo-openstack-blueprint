---
type: Log
title: RabbitMQ users and vhosts as topology-operator resources
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
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
