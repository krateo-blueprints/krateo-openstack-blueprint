---
type: Log
title: services call each other on the internal endpoints, and trove and octavia wait for what they call
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The defect (#462, r074g, 0.3.74 on GKE, 2026-11-23):** trove answered 500 to every instance list, while its component read
  Ready. Two causes, both measured:
  - trove's clients default to `publicURL`, and nothing serves the public endpoints while `publicEndpoints` is off (#451);
  - trove-api started 14 minutes before nova's endpoints were registered. Trove keeps one Keystone session for its process's
    lifetime, so it kept a catalog without nova.
- **The same default was in five more charts, read from their stable/2026.1 source.** A list request doesn't exercise any of
  them, so none was seen failing: masakari's nova client, magnum's seven clients, designate's neutron client, octavia's,
  and mistral's actions.
- **The fix:**
  - all six set the internal endpoints;
  - left public on purpose: heat's `clients_heat`, magnum's `magnum_client` and trove's swift, whose URLs are handed to
    instances outside the cluster;
  - trove depends on glance, neutron and nova; octavia, whose clients are class attributes, also on glance and nova. So
    `spec.enabled: [trove]` now selects nova and what nova needs.
- **Verified:** trove's five options on the r074g cluster (through `componentValues`): 200 with the admin token, 401 without a
  valid one. The other five charts are rendered, not run; octavia is not in full-strict.
- **The guard:** `hack/validate-internal-clients.py` requires the 27 options in the rendered config, rejects any other client
  rendered public without a reason, and checks the dependencies of the services with a long-lived session. The list of
  options is explicit, because the defaults live in the images and no render shows them.
