---
type: Log
title: Start ordering, and re-running hooks (0.3.72, #297, #298, #299)
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Start ordering, and re-running hooks (0.3.72, #297, #298, #299).**
  - **#298:** ceilometer-notification now waits for the rabbit-init Job of every vhost it consumes, as the umbrella
    computes it, overrides included (`conf.notification_vhost_jobs`). Check 26b asserts the jobs equal the vhost list.
  - **#299:** manila's bootstrap waits for keystone, glance, nova and manila-api; the umbrella now creates manila after
    glance and nova. glance's storage-init and octavia's bootstrap now wait for keystone. The new
    `hack/validate-job-service-dependencies.py` maps every `openstack` CLI noun in a Job's script to its service type,
    fails on an unknown noun, and requires the Job's entrypoint to wait for that service.
  - **#297:** umbrella `rerunHooks.<component>` passes a token to that component alone, as `krateo.rerunHooksToken`,
    declared in every component schema and rendered nowhere. It forces the helm upgrade that re-runs hooks a first
    install missed. The new `hack/validate-rerun-hooks.py` checks the schema, that only that component's spec moves,
    the declaration, and that every install hook is also an upgrade hook (static).
