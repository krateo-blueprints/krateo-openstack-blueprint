---
type: Log
title: manila needs a share backend, and the generic driver is refused
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **manila-share never served a share (#426, found by dp303 run 3 and H2 run 5 on 0.3.74-main.ga0e21bf).**
  - **The cause:** upstream's default backend, `generic` (`GenericShareDriver`,
    `driver_handles_share_servers = true`), dies in `init_host` on `FileNotFoundError: [Errno 2] No such file or
    directory: 'ip'`. Its service-instance helper runs `ip`, and no airshipit manila image ships iproute2.
  - **Measured on three images,** without running them: the one 0.3.73 ran, the last jammy pin (0.3.23) and 0.3.74's.
    The binary is absent from the image, not just from PATH, and the same method found dpkg, tr and os-release, so the
    absence is a measurement.
  - **Why it looked fine before:** manila-share had no readiness probe until 0.3.74 (#335, #412). The process stayed
    alive under its name, so it reported `Ready`. The new probe reports "no ESTABLISHED connection to RabbitMQ",
    truthfully, and an install that selects manila never reaches `Ready`.
  - **Also wrong by design:** the generic driver serves shares from Cinder volumes on Nova service instances, which an
    install with `storage.backend: none` cannot give it.
- **The fix (Diego: Manila requires a share backend, Cinder's pattern from #196):**
  - **The profile:** manila's umbrella entry requires `shared-filesystem`, which nothing provides yet (CephFS comes with
    #308), so `profile: full-strict` leaves it out: 31 components, not 32. `profile: full` and `enabled:` still select
    it, and it then refuses to render, as cinder does with no backend.
  - **The chart:** `krateo.shareBackend` defaults to `none`, which refuses, naming what to set. `unmanaged` lets an
    operator configure their own backends under `conf.manila`; any backend whose `share_driver` is the generic driver
    is refused, since it cannot start on these images.
  - **The other gates:** they render manila through `hack/gate_values.py` with one operator example
    (`hack/share-backend-operator-example.yaml`, a CephFS-native backend), so its probes, config files and drop-ins are
    still measured rather than its refusal.
- **The guard:** `hack/validate-share-backend.py`, in capability-roles. It checks that every refusing chart requires
  `shared-filesystem`, and that nothing provides it; that the chart refuses at its defaults, with unmanaged plus
  generic, with an empty backend list, with a backend with no section and with an unknown value; that the operator
  example renders a manila-share workload, and its generic-driver twin is refused; and two mutants (each refusal
  removed), both killed. The preconditions file gains manila's entry, so `hack/validate-provisioning-preconditions.py`
  holds `requires` and the declared precondition to each other.
- **Upgrading:** a `full-strict` install upgraded to 0.3.74 uninstalls its manila Composition. That manila never served
  a share.
