---
type: Log
title: Consume a Ceph cluster without client.admin
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Consume a Ceph cluster without client.admin (#308 phase P1, steps 1-3; rendered and gated, not yet run live).**
  - **Copied keys:** a third credential kind, `source: provided`. With `storage.backend` ceph-rook or ceph-external, the
    umbrella copies each Ceph user's key and `mon_host` from where Ceph keeps them (Rook's `rook-ceph-client-<name>`
    and `rook-ceph-config`, or the operator-named `storage.ceph.keys`) through a SecretStore limited to exactly those
    Secrets. Copies refresh every 5 minutes and are not kept past the release.
  - **Charts:** cinder, libvirt and (with `storage.ceph.glanceStore: rbd`) glance project the copies. No client.admin
    keyring is rendered, no `ceph auth` runs and no pool is created: storage-init checks the pool with the user's own
    key. Each reader carries a live checksum of its key, so it restarts when Ceph's side rotates it. Without a Ceph
    backend every chart renders as before.
  - **One source:** pools, users and the libvirt secret UUID reach cinder, glance, libvirt and nova from
    `storage.ceph` only; `componentValues` may not set them, nor wire Ceph by hand.
  - **Held, refused, never stuck:** a Ceph consumer is created only once the copies of its keys report Ready; the
    Openstack Composition refuses while ESO reports the copy store unable to read the source Secrets, naming the grant;
    glance refuses to move its store to `rbd` off a live `glance-images` claim until `storage.ceph.glanceImagesMigrated`.
  - **Gates:** the new `hack/validate-ceph-consumer.py` (27 copied roots, P projections, 22c reader closure against
    the measured `hack/ceph-readers.yaml`, 18c readers restart, 18d the checksum can read its Secret, 28 no
    client.admin, 29 delivery and consistency through `hack/umbrella_spec.py`, 30 the hold and the read-grant refusal);
    `hack/validate-glance-store.py` adds the migration refusal on the same single claim lookup. `validate-pod-references` renders the storage-dependent charts as the umbrella
    delivers them.
