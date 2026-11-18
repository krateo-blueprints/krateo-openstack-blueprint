---
type: Log
title: The topology projection collided with the chart's connection Secret
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
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
- **The guards, in `validate-messaging-topology.py`:**
  - **Duplicates:** no two resources of a chart's operator-mode render may share group, kind, namespace and name.
  - **One owner:** the imported Secret has exactly one ExternalSecret of its name and no other writer.
  - **2c:** heat with `secrets.oslo_messaging.heat` set to its projection's name must refuse, with that message.
  - A part that checks nothing fails, as before.
- **Not covered here:** duplicates in renders outside operator mode. No gate in this repository checks release-wide
  identity uniqueness for every chart yet.
