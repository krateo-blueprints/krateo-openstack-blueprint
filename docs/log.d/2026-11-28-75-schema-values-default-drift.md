---
type: Log
title: schema defaults and values.yaml agree
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Eight schema defaults differed from values.yaml, or had no values.yaml key.** A Composition gets its defaults from the generated CRD, and
  a plain `helm install` gets them from values.yaml, so one chart could install two ways. A survey of every chart with a
  `values.schema.json` found them in keystone, swift, the openstack umbrella and its generated capabilities chart.
- **The fix keeps the Composition's value in each case.** keystone `conf.keystone.auto_provisioning.enabled`, swift `images.pull_policy`,
  and the umbrella's `enabled` and `tolerations` now carry the value the Composition already used, in values.yaml. For
  `serviceContract.tagging.remediation` the schema default now matches values.yaml, and `serviceContract.ticketing.routing.defaultPriority`
  has no schema default (its description says absent means normal), since values.yaml has no routing block.
- **What a plain `helm install` now renders differently:** keystone's `keystone.conf` gains an `[auto_provisioning]` section, and swift's
  `imagePullPolicy` is `IfNotPresent`, not `Always`. The umbrella and the capabilities chart render byte for byte as before.
- **The capabilities chart's schema is regenerated** with `hack/gen-capabilities-chart.py`, not edited.
