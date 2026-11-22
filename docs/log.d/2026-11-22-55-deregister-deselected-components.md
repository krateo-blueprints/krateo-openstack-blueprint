---
type: Log
title: a component that leaves the selection is deregistered from Keystone, and Skyline tolerates a dead endpoint
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Manila's catalog entry outlived it (#444, found by the 0.3.71 -> 0.3.74 upgrade test r074).**
  - **What happened:** the upgrade removed manila, but its Keystone service `sharev2` (`manilav2`) and its internal and
    admin endpoints stayed in the catalog, pointing at `manila-api`, a Service deleted with the release. manila's
    install Job pods stayed too.
  - **The consequence:** Skyline's `skyline-nginx-generator` writes a literal-host `proxy_pass` per catalog endpoint
    with no `resolver`, so nginx refused to load its config and crash-looped. Skyline never went Ready, and so the
    umbrella never did.
  - **Why the removed chart can't clean up:** `helm uninstall` runs the hooks of the deployed release, manila's
    0.3.71 chart, which deregisters nothing. No change to a removed chart reaches an install upgrading from an older
    release.
- **The fix (Diego's design, 2026-11-22), in two parts:**
  - **Deregistration, from the new release:**
    - **When (a state machine, Diego's design):** on every reconcile the umbrella looks up, by name, each
      not-selected component's Composition and every Service its chart renders. While any exists the component is
      `removing`: nothing is deregistered, and `openstack-deregistration-pending` names what remains. Once none exists
      it is `gone`, and the umbrella passes keystone, as `krateo.deregister`, its Keystone services (minus any type a
      selected component registers) and its name. Nothing is remembered between reconciles, so the order of the
      removal and keystone's upgrade doesn't matter.
    - **A component run on its own is never deregistered:** a standalone Manila next to the umbrella keeps its
      `manila-api` Service, so it stays `removing`.
    - **Where the list comes from:** it is derived from the charts, not kept by hand. `hack/gen-keystone-registrations.py`
      renders each chart's ks-service Jobs into `osh.keystoneRegistrations`, and each chart's Services into
      `osh.componentServices`.
    - **The hook:** keystone's new post-install/post-upgrade Job, `keystone-deregister`, deletes each such service
      with its endpoints, only when type and name both match. It also deletes those components' leftover hook Jobs,
      with background propagation.
    - **Idempotent:** it covers upgrades from any older release and a later deselection by a user.
  - **A tolerant Skyline:** after the generator, skyline's init rewrites each endpoint location to resolve per request
    through the cluster DNS. The `resolver` comes from the pod's resolv.conf. The URI semantics are unchanged: the
    rewrite to the endpoint path plus a variable host. A host that does not resolve now answers that location with
    502, and nginx starts. Upstream divergence `skyline-444`.
- **The guards:**
  - **`hack/validate-deregistration.py`:**
    - the table is current, manila registers sharev2, and no type has two owners;
    - keystone's list for each profile, a must-fail check on #444's catalog, and the catalog and jobs programs run
      against stand-ins;
    - the state machine, with its lookups stood in: gone (handed over), removing (the Composition exists) and
      standalone (only `manila-api` exists), both kept;
    - 6 mutants, including the Service lookup and the Composition check each removed.
  - **`hack/validate-skyline-nginx.py`:** upstream's template is rendered with an unresolvable host. `nginx -t` fails
    before the rewrite and passes after it, and a running nginx proxies the same path and answers the dead host 502.
    3 mutants.
- **Upgrading:** nothing to do. The first upgrade to this release runs keystone's hook, which removes what earlier
  removals left. A Keystone Composition whose migration is not approved (pinned) receives no `krateo.deregister`
  (README).
