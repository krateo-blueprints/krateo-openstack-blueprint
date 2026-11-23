---
type: Log
title: Every selected component renders as the umbrella delivers it
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The delivered pass of `validate-pod-references.py` now shows and requires what it rendered (#309 H2, after
  #397).**
  - **Why:** #397 added a pass that renders each component as the umbrella delivers it, per profile and per selection,
    for `hack/render_identity.py`. It printed no line per render, so a log could not show that ceilometer, refused at
    #397's first head with a spec no install delivers to it, rendered under the profiles that select it. Nothing
    failed when a selected component had no delivered render.
  - **Visibility:** each delivered render prints `OK <component> (as the umbrella delivers it, profile …): identity
    only`.
  - **Completeness:** the gate fails when a component some profile selects has no successful delivered render.
    Storage-dependent charts are rendered per storage.backend instead.
  - **The planted case:** `GATE_PLANT_DROP_DELIVERED=<component>` skips that component's delivered renders, as a
    future change to the modes could. `hack/gates.d/validate-pod-references.yaml` runs the gate on ceilometer alone
    with it, and requires rc ≠ 0 and the line naming ceilometer, not merely a failure.
  - **A mutant, not a bypass:** while the variable is set, the gate exits non-zero whatever it found ("PLANTED MODE …
    can never pass"). Set by mistake in CI, it fails instead of passing on less. The repo-wide rule for environment
    reads in gate scripts is issue #398.
