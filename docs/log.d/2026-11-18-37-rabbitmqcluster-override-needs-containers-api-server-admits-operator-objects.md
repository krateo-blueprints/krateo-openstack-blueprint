---
type: Log
title: RabbitmqCluster override needs containers; an API server admits the operator objects
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The operator-mode RabbitmqCluster was refused by its CRD (#309 H2).** H2's live test on GKE installed
  0.3.74-main.g92cd983 with `messaging.backend: operator`. The rabbitmq Composition then stayed Ready=False with
  `RabbitmqCluster.rabbitmq.com "openstack-mq" is invalid: spec.override.statefulSet.spec.template.spec.containers:
  Required value`, so no broker was ever created, and every component that waits on messaging waited behind it.
  - **The cause:** the template's override PodSpec held only a `nodeSelector`. The cluster-operator's CRD types that
    field as a whole PodSpec, where `containers` is required.
  - **The fix:** the override lists one container, `rabbitmq`, by name only. The operator merges the override into
    its own PodSpec strategically, containers by name (v2.23.2 `internal/resource/statefulset.go`, `patchPodSpec`),
    so the entry changes nothing else. Without any entry the override would marshal `containers: null`, which the
    merge would read as deleting them; the CRD refuses that case before it can happen.
  - **Why every gate was green:** `validate-messaging-operator.py` and `validate-messaging-topology.py` render the
    objects and check this repository's own predicates. Nothing asked the CRD.
- **The guard: `hack/validate-operator-admission.py`, in a new lint job `operator-admission`.**
  - **The CRDs:** read from rabbitmq-operator-blueprint at the commit of the operator build H2 runs (e22d03a), with a
    sha256 per upstream manifest (`hack/operator-crds.yaml`). There is no copy here to drift.
  - **The objects:** the two messaging gates write every rabbitmq.com object they render from the umbrella's operator
    spec to `OPERATOR_OBJECTS_DIR`, never one from a mutation. Both gates must pass, and each required kind
    (RabbitmqCluster, User, Vhost, Permission, Policy) must have at least one object.
  - **The validator:** a kind cluster serving those CRDs dry-runs every object server-side. The API server validates,
    not a schema library, which would be a second implementation that disagrees at the edges.
  - **The controls:** per kind, one admitted object is broken and must be refused: the RabbitmqCluster without its
    override `containers` (this defect), each other kind without the first field its CRD requires.
  - **Also:** `validate-messaging-operator.py` asserts the override lists exactly `rabbitmq`, and the rabbitmq
    divergence entry asserts the three lines.
  - **Out of scope:** the operators' admission webhooks and what the operators do after admission. The H2 live test
    covers those.
