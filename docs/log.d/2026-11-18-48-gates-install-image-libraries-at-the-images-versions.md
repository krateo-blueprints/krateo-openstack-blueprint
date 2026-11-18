---
type: Log
title: Gates install image libraries at the images' versions
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Gates install image libraries at the images' versions.**
  - **What happened:**
    - oslo.service 4.9.0 reached PyPI on 2026-11-18. It initialises the eventlet backend on import, so main's
      worker-probe gate, which installed oslo.service unpinned, failed on every pull request until #412 pinned it.
    - Two other gates pinned versions the images don't ship. validate-horizon-backchannel-tests pinned Django 5.2.19
      and cryptography 46.0.5, but the horizon image ships 4.2.28 and 43.0.3. validate-nova-compute-liveness pinned
      psutil 7.0.0, but the nova image ships 7.2.2.
  - **The fix:** the three gates install with `pip install -c "$(python3 hack/constraints.py url)" <names>`. That is
    openstack/requirements stable/2026.1's upper-constraints at commit ecafdea7, kept in
    `hack/openstack-constraints.yaml` with the `git ls-remote` command that resolved it. Every transitive dependency is
    then the images' own too.
  - **Versions:** every constraint equals the version measured in the 2026.1-ubuntu_noble-20261117 images:
    - oslo.service 4.5.1, cotyledon 2.2.0, setproctitle 1.3.7, oslo.messaging 17.3.2, eventlet 0.40.4;
    - oslo.config 10.3.0, kombu 5.6.2;
    - Django 4.2.28, cryptography 43.0.3, psutil 7.2.2.
  - **Checks:**
    - Each gate first runs `hack/constraints.py check <names>`, which fails when a name is missing, unconstrained or
      at another version.
    - The worker-probe gate also runs `constraints.py self-test oslo.service`. It installs oslo.service unconstrained
      in a throwaway venv, gets 4.9.0, and requires `check` to refuse it.
    - The 72 back-channel logout tests pass on Django 4.2.28.
  - **New gate `hack/validate-pip-constrained.py`:** a gate that pip-installs anything but PyYAML must use the
    constraints, carry no version of its own, and start its run with `check` on exactly those names. Run against
    main's old gate files, it fails all three.
