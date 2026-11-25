---
type: Log
title: cinder-volume can start: its internal tenant is created before bootstrap waits for it
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **cinder was never Ready with a backend (#308 run A, GKE, Ceph through rook-ceph-blueprint, 2026-11-25).** The Ceph side
  worked: cinder-storage-init and glance-storage-init listed their pools with the copied keys. But cinder-volume's init
  container init-cinder-conf failed on `No Project found for internal_cinder`, and cinder-bootstrap, a post-install hook at
  weight -7, waited for a Ready cinder-volume pod. They waited on each other.
  - **Cause:** only the create-internal-tenant Job creates that project, and our values had it off since cinder was vendored
    (#3). Upstream ships it on. No earlier run had a block-storage backend, so cinder-volume never started.
- **The fix:** the Job is on. It runs in the first hook wave (-10) and waits only for keystone-api. It uses the generated
  admin and cinder Secrets, so no published credential is added (#228).
- **The guard:** `hack/validate-cinder-internal-tenant.py`, rendered as the umbrella delivers cinder. When a cinder-volume
  workload reads internal_cinder, the Job must render, as a post-install and post-upgrade hook below every hook that waits
  for cinder-volume, and must not wait for cinder-volume itself. Its self-test fails on main's values.
