---
type: Log
title: Bootstrap uploads each image once; glance's store to 10 GiB (0.3.71, #295)
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Bootstrap uploads each image once; glance's store to 10 GiB (0.3.71, #295).**
  - **What happened** (0.3.68 → 0.3.70 upgrade on GKE): manila's bootstrap ran upstream's `image show || create` while
    glance-api was rolling, and read "glance unreachable" as "missing". It uploaded a second 952 MB manila-service-image,
    which filled the 2 GiB store. glance went unready on its free-space floor (#163), and glance, manila and nova
    stalled.
  - **Bootstrap fix:** glance's, manila's and ironic's bootstrap, and ironic's cleaning-network Job, now count by name
    (helm-toolkit `openstack_count`). A failed lookup fails the Job, and nothing is created when one or more already
    exist.
  - **Store:** a new `glance-images` claim requests 10Gi (`volume.defaultSize`). An existing claim keeps its size on
    upgrade (Diego, 2026-11-17), so no upgrade patches a claim nobody asked to change, and kind's local-path class (no
    `allowVolumeExpansion`) can't fail one. `volume.size`, empty by default, grows the claim and never shrinks it.
  - **New gates:** `hack/validate-bootstrap-idempotent.py` (no create after a failed lookup unless the API refuses
    duplicates; image bootstraps run against a stand-in) and `hack/validate-glance-store.py` (the size each live claim
    gets).
