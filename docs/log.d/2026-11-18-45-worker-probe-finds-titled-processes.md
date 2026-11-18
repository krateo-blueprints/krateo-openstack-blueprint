---
type: Log
title: The worker readiness probe works on titled processes
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The worker readiness probe works on titled processes (0.3.74 defect B, #231).** 0.3.74 is blocked on this. The
  probe is new in it (#335), and no released version ever ran it.
  - **What happened** (GKE, 0.3.74-main.g0e23f7a): 14 RPC workers stayed Running but never Ready, each with
    `not ready: no <program> process is running in this container`. The services affected: designate, heat, mistral,
    neutron-rpc-server, cyborg, cloudkitty, aodh's listener and notifier, and manila-share. Their RPC worked.
  - **The cause:** the 2026.1 images ship setproctitle. Two things call it:
    - oslo.service's threading backend, which builds a cotyledon ServiceManager under both of its launchers;
    - cotyledon itself, which aodh, ceilometer and cloudkitty use directly.

    It rewrites each process's `/proc` cmdline into one title, such as `designate-central: master process [...]` and
    `designate-central: ServiceWrapper worker(0)`, and its environ with it.

    The probe found the program by an argv element's basename, and read its `--config-file` from that argv, so on
    titled services it found nothing. The eventlet backend sets no title, which is why blazar and others passed.
    Rosetta (kind on Apple Silicon) sets the title but doesn't rewrite cmdline, so it hides the defect. The probe's
    tests used plain argv, which is why CI passed.
  - **The fix:**
    - The program is now the container's processes: the probe's mount namespace, minus the probe. `--process` only
      labels messages. Readiness is still the RabbitMQ connection plus the RPC answer.
    - The configuration is passed to the probe, never read back. Each program's ordered list lives in one place,
      `<chart>.config_args.<program>` in its chart, with its template conditionals. `helm-toolkit.snippets.oslo_config_args`
      renders it into both the start script's exec line and the probe's command.
    - `--config-dir-if-present` is the probe's form of the scripts' `$([ -d D ] && echo --config-dir D)`.
    - ironic-conductor's `${OPTIONS}` files moved into its list.
  - **Proof the refactor changes no program's arguments:** this covered all 18 probed charts, rendered before and
    after:
    - at their defaults;
    - as the umbrella delivers them in identity, full and full-strict;
    - with variants that flip every conditional in the lists.

    In all 163 rendered start scripts, everything outside the program's invocation is byte-identical. Bash evaluated
    each invocation with every conf.d present and then absent: 326 argv identical, except aodh's `--config-file=X`,
    which became `--config-file X`.
  - **New gate `hack/validate-probe-config-args.py`:** every probe's list must equal its exec line's, evaluated by
    bash, in each chart as the umbrella delivers it. It runs two positive controls.
  - **Probe tests:** `hack/validate-health-probe.py` now starts real processes through oslo.service's threading
    backend (one and two workers), cotyledon directly, the eventlet backend, and a plain process, at the image
    versions. It checks ready and not ready end to end. It fails, never skips, on a host that doesn't rewrite cmdline.
    It kills three mutants: argv name matching, config read from argv, and the mount-namespace filter dropped (real
    with `sudo unshare -m`, otherwise a stub it names).
