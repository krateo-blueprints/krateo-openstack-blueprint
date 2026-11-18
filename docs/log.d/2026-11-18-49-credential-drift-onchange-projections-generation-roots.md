---
type: Log
title: A hand-edited root is reported, projections stop following roots, and rotation mints new roots
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Editing a root was an uncoordinated rotation (#309 C1).** H2 run 4's P3 patched a RabbitMQ root, and External
  Secrets Operator regenerated it within a second. v2.14.0 skips a refresh only while the Secret's data matches its
  data-hash annotation (`externalsecret_controller.go:370`, `isSecretValid`), under Owner whatever the refresh policy.
  RabbitMQ's projection then moved at once and heat's transport drop-in an hour later, and heat was refused 1894 times.
  Any hourly projection carried such an edit into one side of a credential while the backend kept the other.
- **Projections stop following roots:**
  - **The change:** every projection of a generated root is `refreshPolicy: OnChange` with no interval: 25
    templates, 13 in helm-toolkit, 12 in component charts, plus the umbrella's messaging projections. The Ceph copies
    stay Periodic 5m, since their owner rotates them by design.
  - **Measured on ESO 2.14.0 in kind** (`validate-eso-onchange-switch`, operator-admission job):
    - the switch leaves the Secret byte-identical;
    - OnChange ignores a root change that a 10 s Periodic twin follows;
    - it refreshes on its own change, follows a root rename, and syncs once a missing root appears.
  - **Nothing relied on in-place changes:** every release-driven rotation (0.3.53-0.3.68) introduced a new root, so
    its projection was new or its spec changed.
  - **Guard:** `validate-pod-references` checks 270 projections and plants an hourly one.
- **A hand-edited root is reported:**
  - **Detection:** the umbrella latches a sha256 of each generated root (ConfigMap
    `openstack-credential-generations`); ESO's own SHA3-224 data-hash can't be computed in a template.
  - **The report:** a change is listed on `status.credentialDrift` by Secret name, with the current sha in ConfigMap
    `openstack-credential-drift-<Secret>`.
  - **Acknowledging it:** `credentialDrift.acknowledge.<Secret>` takes that sha, and only that sha.
  - **Guard:** `validate-credential-drift`.
- **Rotation mints a new root.** `rotate.oslo-messaging-<service>`, with `messaging.backend: operator` only, typed for
  the 20 messaging ids that rotate, names a new root, `<base>-g<first 8 hex of sha256(token)>`, minted once.
  - **Why not OnChange roots:** switching the existing roots to OnChange would itself be a spec change, and ESO would
    regenerate every root at the upgrade.
  - **Derived from what exists:** the active root is what the live Composition references, and the previous one is
    stamped on the new root's ExternalSecret, so no ConfigMap is needed.
  - **Refused, naming why:** a reused token, a removed token, and a token in chart mode.
  - **Guard:** `validate-credential-rotation`.
  - The second user, the switch and the cleanup are #309 C2.
