---
type: Log
title: a gate's download is tried again when the server says not now
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The failure:** raw.githubusercontent.com answered 503 (`Backend.max_conn reached`) to a gate's only request in #458, #324
  and #469, on 2026-11-23 and 24. Each failed a capability-roles job that had nothing to do with the change.
- **The fix:** `hack/fetch.py`, used by `hack/constraints.py` and `hack/validate-operator-admission.py`. 429, 5xx and network errors are
  tried again, five times in all over 73 s. A 404 fails at once, and the last error is raised.
