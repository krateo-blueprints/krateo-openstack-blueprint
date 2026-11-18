---
type: Log
title: The operators' real webhooks admit every operator-mode object
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Phase 2 of `validate-operator-admission.py`: the operators' admission webhooks in CI's kind (#405).** Three H2
  live runs on 2026-11-18 each stopped on an admission-class defect that every offline gate passed: the CRD schema
  (#393), a duplicate identity (#397), and the topology operator's User webhook (#404). `operator-admission` covered
  the first class; phase 2 covers the third.
  - **Setup:** in the same kind cluster, after the CRD-only phase, it installs cert-manager v1.21.3 (URL and sha256
    pinned in `hack/operator-crds.yaml`) and both operator manifests whole, from the pinned bytes the CRDs come from.
    It waits until both webhooks answer.
  - **The Secrets:** for every ExternalSecret the messaging gates render, it creates the Secret ESO would write, using
    `hack/eso_model.py`. That is one function: target name, type, labels and annotations, plus ESO's managed label,
    and the template's keys or spec.data's, with placeholder values. Its self-test runs first, against ESO's
    documented target semantics, and it refuses the forms it does not model (dataFrom, templateFrom, mergePolicy
    Merge, creationPolicy Merge or None).
  - **The check:** every rabbitmq.com object is dry-run again with the webhooks active. Two retries, 5 s apart, cover
    only a Secret not yet in the operator's cache.
  - **The controls,** each refused for its own reason:
    - a User importing a Secret without `rabbitmq.com/topology-operator` ("must have label");
    - a User importing an absent Secret ("not found");
    - a RabbitmqCluster whose override sets `hostNetwork` ("hostNetwork is not permitted").
  - **The limit:** the Secrets are eso_model's, so a check built on them cannot validate ESO itself. The H2 live runs
    remain the check on that part, and on everything after admission.
