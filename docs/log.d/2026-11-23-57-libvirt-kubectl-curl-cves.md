---
type: Log
title: libvirt's kubectl image repinned past 20 CRITICAL curl findings; linux-libc-dev findings allowlisted until 2026-12-23
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The image scan turned red on the 0.3.74 candidate (image-scan run 45098584294, main 5bc3e765, 2026-11-23).** The
  morning's scan of the same commit was green. 35 CRITICAL findings had been published since.
  - **20 in `docker.io/bitnami/kubectl`:** curl and curl-libs 8.22.0, 10 CVE/BDSA ids, fixed upstream in 8.23.0. libvirt
    declares this image for its `cert-init-vnc` init container, which renders only with `conf.qemu.vnc_tls: "1"`. The
    default is `"0"`, so the image is `declared-only` in the inventory.
  - **15 in `linux-libc-dev`:** CVE-2026-99287, -99306 and -99310, in barbican's and the four kolla gnocchi images.
    Trivy reports no fixed version.
- **Diego's decision: fix where a fix exists, allowlist only what has none.**
  - **The fix:** libvirt's `images.tags.kubectl` is pinned to `bitnami/kubectl@sha256:ae71f848…`, the `latest` index Bitnami
    published 2026-11-22T20:44Z. The previous digest, `bedd34c9…`, was pinned on 2026-10-30 (#70). This pull request's
    image scan scans the new digest; it must be free of the curl findings.
  - **The allowlist:** `hack/image-cve-allowlist.yaml` gains 15 dated entries for `linux-libc-dev`, expiring 2026-12-23.
    It is the kernel's userspace headers, which no process in those images runs. Remediation is tracked in #454.
- **For 0.3.74:** only the libvirt chart changes. The tag moves to this commit, after a new pre-release and the fresh-install
  run, with chartdiff showing libvirt as the only changed chart.
