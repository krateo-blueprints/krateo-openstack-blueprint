---
type: Log
title: Images pinned; remediation issues (#316, #328 to #334)
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Images pinned; remediation issues (#316, #328 to #334).**
  - **Dated tags:** Diego decided on 2026-11-17 to pin dated tags and verify their digests in CI. The 31 airshipit
    release-series images now use `2026.1-ubuntu_noble-20261117`, and `docker.io/rabbitmq:3.13-management` became
    `3.13.7-management`. Each new tag serves the digest that was recorded and scanned, so no bytes change. It is also
    the newest dated build, so no baseline CVE is fixed and the allowlist keeps all 167 entries, re-keyed.
  - **libvirt:** appVersion was re-measured on its dated image: libvirt 10.0.0, from Trivy's package list.
  - **Gates:**
    - `hack/gen-image-inventory.py` fails on a release-series tag unless `hack/image-pin-waivers.yaml` waives it, and
      on a stale or expired waiver.
    - With `--resolve` (image-scan), it fails when a pinned tag serves a digest other than the recorded one, and
      `--write` keeps the recorded digest.
    - `validate-app-version.py` and `validate-keystone-oidc-image-series.py` accept the dated suffix and compare the
      series.
  - **Mirror:** `hack/image-mirror.yaml` and `.github/workflows/image-mirror.yaml` copy the 4 Kolla gnocchi images by
    digest to dated tags in `ghcr.io/krateo-blueprints/images/`. The copy runs on main or by hand. It never moves a
    tag, and it fails until the package pulls anonymously. The 4 images are waived until 2027-01-31 (#334).
  - **Remediation issues:** #328 (PyJWT, from `stable/2026.1` upper-constraints), #329 (nginx 1.13.3 in ironic),
    #330 (drop the rally test pods), #331 (docker 27.5.0 and image_repo_sync), #332 (PowerDNS), #333 (memcached) and
    #334 (Kolla gnocchi). Candidate replacements were scanned, so each issue states the CRITICAL count the fix leaves.
