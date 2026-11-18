---
type: Log
title: cinder's default refuses; the gates render it as the umbrella delivers it
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **cinder's default refuses; the gates render it as the umbrella delivers it (#308 step 4, #196).**
  - **The flip:** cinder's chart default for `krateo.storageBackend` is now `none`, which refuses to render, instead
    of `unmanaged` (upstream's `rbd1` wiring). Through the umbrella nothing changes, since it always passes
    `storage.backend`. A standalone Cinder Composition must choose: its refusal names the key and the two escapes,
    `unmanaged` and `ceph-external` (README, "Upgrading: a standalone Cinder Composition must choose its storage
    backend"). Diego chose this, keeping `unmanaged` as an explicit opt-in.
  - **Why:** #196 stayed open for a standalone Cinder Composition, which rendered upstream's wiring silently and waited
    in `Init` forever without hand-provided Ceph objects, including `client.admin`.
  - **Gates:** the new `hack/gate_values.py` hands a chart that requires block-storage exactly what
    `storage.backend: ceph-external` adds to its delivered spec, computed from the umbrella's own lines through
    `hack/umbrella_spec.py`; every other chart gets nothing, so its render is unchanged. 25 gates that render every chart
    pass it (measured: all 25 rendered cinder at its default and failed when the default was flipped without it). A
    renderer that misses it renders cinder's refusal and fails. `validate-storage-backend` check 3 and the cinder
    divergence entry assert the new default.
