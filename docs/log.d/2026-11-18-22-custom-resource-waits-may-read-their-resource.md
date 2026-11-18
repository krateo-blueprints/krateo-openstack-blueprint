---
type: Log
title: Custom-resource waits may read their resource
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Custom-resource waits may read their resource (#309).** helm-toolkit's `kubernetes_pod_rbac_serviceaccount` now
  renders, for a component whose dependencies list `custom_resources`, a Role granting `get` on exactly each named
  object (its group, its `resource` plural, `resourceNames`) and a RoleBinding to the pod's ServiceAccount.
  kubernetes-entrypoint (pinned `c606628c`) already waits on custom resources by `status.conditions`, but the dependency
  Role covered only core, batch, apps and discovery resources, so every such wait would have been Forbidden. An entry
  without `resource`, or with a core apiVersion, is refused at render time. No chart declares `custom_resources` yet:
  every render is unchanged. It unblocks the operator CRs that replace db-init and rabbit-init (#309, H2b/H3b). New gate:
  `hack/validate-custom-resource-wait-rbac.py`.
