---
type: Log
title: The umbrella writes each RabbitMQ user projection and creates the component after it
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The first-User race is gone by construction (#309 H2, Diego's option 4).** H2 run 4 on GKE measured every
  messaging component, 15 of 15, refused on its first install by the topology operator's User webhook (`secret
  "<svc>-rabbitmq-topology-user" not found`), 1-2 times each.
  - **The cause:** its chart submitted the projection ExternalSecret and the User in one helm transaction, and ESO
    wrote the Secret 3-85 s later.
  - **The cost:** CDC's retry admitted the User up to 198 s after the Secret existed, and the refusal stayed on the
    status for minutes.
  - **Ruled out:** a helm hook (helm v3.20.2 waits for no ExternalSecret or Secret), the operator itself (v1.20.5 has
    no ordering, only `ENABLE_WEBHOOKS=false` or generated credentials), and a `lookup` of the root (the password
    would land in the release).
- **The change:**
  - **Who writes it:** with `messaging.backend: operator`, the umbrella renders `<component>-rabbitmq-topology-user`
    for each selected component whose rabbit-init runs, from its root, labelled for the operator.
  - **When the component is created:** only once `osh.lookupSecret` finds that Secret, labelled
    (`osh.messagingProjectionReady`, beside the dependency and Ceph gates; an existing component is never held).
  - **The chart:** it imports the name it is passed (`endpoints.oslo_messaging.operator.userSecret`) and renders no
    projection. A standalone install keeps the chart-rendered one and CDC's retry.
  - **One rule:** `osh.runsRabbitInit` is the rabbit-init rule, read by both the notification vhosts and the
    projection.
- **The guards:**
  - `validate-messaging-topology.py` requires the umbrella as the one writer, with the chart's root, key and username,
    and the label.
  - `validate-messaging-operator.py` part 5 renders the gate with a stand-in for `osh.lookupSecret`. helm template's
    lookup returns nothing, so an offline render emits no gated component. It covers 4 cases, plus the creation
    condition's structure.
  - `validate-ceph-consumer.py` check 30 now asserts the creation gate's structure, not its text.
  - Mutants killed: the gate dropped, the label ignored, `$cephReady` dropped.
