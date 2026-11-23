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
- **The guards:**
  - **`hack/render_identity.py`:** no two resources of one render may share group, kind, namespace and name. The gates
    that already render every chart pass their output through it, rather than render the charts again:
    - `validate-pod-references.py`: every component chart at its defaults, under each storage.backend it depends on,
      and, new, as the umbrella delivers it: for each profile, the spec compositions.yaml builds for each component
      that profile selects (`umbrella_spec.selected`, new), each distinct spec rendered once. The selection matters:
      ceilometer's spec under identity, which does not select it, has no notification vhosts, and the chart refuses
      it by design (#281).
    - `validate-messaging-topology.py`: every messaging client in operator mode.
    - `validate-messaging-operator.py`: the rabbitmq chart (3 replicas and 1) and every client in operator mode.
  - **The ledger:** each gate reports the renders and resources checked, and fails when either is 0. Once per gate, a
    planted duplicate of the first resource rendered must be found.
  - **One owner:** `validate-messaging-topology.py` also requires exactly one ExternalSecret of the imported name and
    no other writer of its Secret.
  - **2c:** heat with `secrets.oslo_messaging.heat` set to its projection's name must refuse, with that message.
