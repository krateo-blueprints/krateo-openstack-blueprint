---
type: Log
title: gnocchi's resources-cleaner runs; every rendered script starts with #!
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **gnocchi's resources-cleaner CronJob had never succeeded (#490).** Found on the 0.3.75 candidate's clean cluster (main 604f0bc8, GKE), the
  first of our runs still up at 00:00 UTC, its schedule. Each attempt: `exec format error`, exit 255.
  - **Cause:** its container's command is the script itself, so the kernel executes it. The script had no `#!/bin/bash` line: lost when the
    chart was vendored (#23); upstream has it. 0.3.74 has the same template.
  - **Proof:** the CronJob's own pod spec, run through `bash`, succeeded on that cluster.
- **The same defect, not failing today:** 12 of 343 rendered scripts. heat's `trusts.sh` has no line, and ten swift scripts have it after a
  blank line (`{{- define … }}` without `-}}`), which the kernel treats the same. They run because their callers start bash. Upstream has the
  same heat and swift templates.
- **The fix:** the line in gnocchi's and heat's scripts; swift's defines trimmed.
- **The guard:** `hack/validate-bin-scripts.py` refuses any rendered `.sh` key whose first two bytes are not `#!`, with a self-test.
