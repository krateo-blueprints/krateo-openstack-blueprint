---
type: Log
title: cinder refuses to render without a block-storage backend
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **cinder refuses to render without a block-storage backend (#196, phase P0 of #308).**
  - **What was wrong:** cinder's only backend is Ceph RBD. With no Ceph, `cinder-volume` and `cinder-storage-init`
    waited in `Init` forever on `ceph-etc` and keyring Secrets nothing creates. That was the state on every default
    install (#179, #204).
  - **Now:** the Openstack Composition has `storage.backend`, `none` by default, typed by the generated schema. The
    umbrella passes it to every component that requires block-storage (cinder) as `krateo.storageBackend`, and refuses a
    `componentValues` override. With `none`, cinder fails its render and names the value. The Ceph backends
    (`ceph-rook`, `ceph-external`) are accepted by the schema and refused by cinder until #308 implements them.
  - **Selection:** `requiresProvisioning` became `components[].requires`, a capability list (`block-storage`,
    `pxe-interface`, `lb-mgmt-net`, `rwx-storage-class`). `full-strict` selects a component only when the install
    provides everything it requires; only `block-storage` can be provided, by `storage.backend`. `full` is unchanged.
  - **Gates:**
    - `hack/validate-provisioning-preconditions.py` also renders `full-strict` with every non-`none` backend. That is
      the positive control: it must add cinder and nothing else.
    - The new `hack/validate-storage-backend.py` checks that the value reaches cinder alone, that the umbrella is the
      one place that sets it, and that cinder refuses without a backend.
    - `hack/validate-pod-references.py` renders cinder once per backend the umbrella can deliver. Its #196 allowlist
      entries are gone.
  - **Also:** nova-compute's `LIBVIRT_CEPH_SECRET_UUID` read the undefined `conf.ceph.secret_uuid` (an upstream defect),
    so it rendered with no value. It now reads `conf.ceph.cinder.secret_uuid`, and the new gate renders it with a
    sentinel.
