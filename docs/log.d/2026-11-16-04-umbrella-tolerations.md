---
type: Log
title: Umbrella `tolerations` (0.3.68)
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Umbrella `tolerations` (0.3.68).** One list on the Openstack resource reaches every pod of every component, as
  OpenStack-Helm's `pod.tolerations.<component>`. A trusted-tenant vcluster copies the pool taint
  `krateo.io/trusted-tenant=<ns>:NoSchedule` onto its nodes, so without it every DaemonSet desired 0 pods. Nine charts
  had no `pod.tolerations` block (cyborg, designate, magnum, masakari, mistral, octavia, powerdns, redis, watcher), and
  workloads and helm-toolkit Jobs in others never applied it. `hack/validate-tolerations.py` renders every chart with
  every workload manifest on and requires exactly the list on every pod template.
