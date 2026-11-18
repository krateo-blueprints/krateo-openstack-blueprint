---
type: Log
title: image-scan retries a scan cut short by a layer download (#316, #334)
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **image-scan retries a scan cut short by a layer download (#316, #334).**
  - **What failed:** image-scan run 44202269356 (on `feat/309-h0-h1`) failed in
    `scan (quay.io/openstack.kolla/gnocchi-metricd:2026.1-ubuntu-noble)`. The cause was no new CVE: Trivy stopped
    with `failed to analyze layer ... unable to read the file: unexpected EOF`, a layer download cut short. The gate
    then failed, correctly, on the image's missing report.
  - **Measured on 2026-11-18:** the waived series tag still serves the recorded digest (`sha256:12c53f1f…`), so it
    had not moved upstream. Main's image-scan on `08ec684`, a few minutes later, scanned the same digest with no
    finding outside the allowlist.
  - **The fix:** the scan step now tries up to three times and drops only the layer-analysis cache between attempts.
    A real finding can't be retried away, because findings are judged from the report. An image that fails all
    three attempts still has no report, and the gate fails it by name.
  - **The allowlist doesn't change:** there was no new finding, and entries are only made for measured findings.
