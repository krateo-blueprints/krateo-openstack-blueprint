---
type: Log
title: The credentials store may read the generation roots a RabbitMQ rotation mints
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **A rotation stopped in minting (H2 run 5, defect H, 2026-11-19).** The README procedure
  (`rotate.oslo-messaging-heat: <token>`) minted the generation root `openstack-credential-oslo-messaging-heat-g<8 hex>`,
  and the umbrella rendered its projection. The projection then stayed `SecretSyncedError` ("could not get secret data
  from provider"): the SecretStore's identity, `<credentials.store>-reader`, may `get` only the Secrets its Role lists by
  `resourceNames`, and the list held only `credentials.items`. The new User was never created, so nothing switched;
  heat kept working on generation 0.
- **The fix:** one helper, `osh.generationRoots`, lists the generation roots a render may read: each `rotate.<id>`
  token's root, and the root stamped on it as its predecessor (`krateo.io/previous-root`), which a second rotation still
  drains. The reader Role (`templates/credentials.yaml`) and the drift latch (`osh.credentialDrift`) both read that one
  list, so the grant and the latch cannot disagree about which roots exist.
- **Why the gates missed it, and the guards now:**
  - The rotation gates stood in for ESO, and a stand-in cannot see a missing RBAC grant (the same lesson as #393 and
    #397: rendered is not admitted).
  - `hack/validate-messaging-rotation.py` part 2 now compares the rendered Role's `resourceNames` with every root a
    rendered projection reads through the store, while minting and during a second rotation that drains the first; a
    mutant dropping the generation roots from the Role fails it.
  - `hack/validate-eso-onchange-switch.py` step 7 measures it on the real ESO 2.14.0 in kind: a store whose Role has the
    umbrella's exact shape (get by resourceNames) but omits a root refuses that projection with `SecretSyncedError`;
    with the root added, the same projection syncs with no change of its own. So the list is what decides, and the
    render check measures something real.
- **Verified:** locally, the rotation gate (9 umbrella checks, 8 mutants killed) and the ESO gate (steps 0 and 3–7);
  live, the P3 re-test of the README procedure is H2 run 6.
