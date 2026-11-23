---
type: Log
title: The topology projection labels its Secret for the operator
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The topology operator refused every User (#309 H2, run 3).** On GKE, 0.3.74-main.g6d894b3 admitted the topology
  CRs live for the first time. Vhosts (11/11), Policies (3/3) and the administrator's Permissions went Ready. Every
  User was refused by the operator's webhook: `secret "heat-rabbitmq-topology-user" must have label
  rabbitmq.com/topology-operator=true to be used by the Topology Operator`. The user Permissions then waited on
  "missing User".
  - **The cause:** messaging-topology-operator v1.20.5 caches only Secrets carrying that label (`cmd/main.go:281-283`,
    to bound memory). Its User webhook refuses an import Secret that exists without it, or doesn't exist
    (`internal/webhook/v1beta1/label_validation.go`). Its controller watches Secrets through the same cache
    (`internal/controller/user_controller.go:385`), so an unlabelled projection would also never re-sync a rotated
    root. The projection's ExternalSecret set no labels, and ESO wrote only its own.
  - **The fix:** the projection sets `spec.target.template.metadata.labels: rabbitmq.com/topology-operator: "true"`,
    which ESO stamps onto the Secret.
  - **The guard:** `validate-messaging-topology.py` asserts the label on every projection.
  - **Why the gates missed it:** #393's admission gate validates against the CRDs and runs no operator webhook. The
    class guard, the operators' real webhooks in CI's kind against every operator-mode object, is the next pull
    request.
