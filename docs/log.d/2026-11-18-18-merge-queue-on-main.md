---
type: Log
title: Merge queue on main, drafted
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Merge queue on main, drafted.** Diego approved GitHub's merge queue (2026-11-18), so every commit that lands is
  tested as it will exist on main. Until now each pull request was brought up to date with main and waited for, one
  at a time. Nothing is applied to the repository settings yet.
  - **lint.yaml** runs on `merge_group`. The job-hooks base comes from `merge_group.base_sha`.
    `hack/validate-log-untouched.py` and `hack/validate-no-skip-markers.py` read a merge group commit by commit, as
    a push. Each queued pull request is its squash commit there, so the message that would land is checked.
  - **The ruleset draft**, `hack/merge-queue-ruleset.json`, adds a merge queue (squash, ALLGREEN, groups of up to 5,
    a 60-minute check timeout) and requires the 12 checks lint.yaml reports, from GitHub Actions. The informational
    `lint` job is left out. `hack/validate-merge-queue-ruleset.py` fails when the two drift apart.
  - **Not gating:** image-scan (it scans every image, not the changed ones) and the security workflow.
  - **Releases:** `hack/require-lint-passed.sh`, extracted from release-tag.yaml, accepts a successful lint run on
    the exact commit from its merge group, its push to main, or a dispatch. A pull request's run is no longer
    counted, and each run's `head_sha` is re-checked. `hack/validate-require-lint-passed.py` covers 8 cases.
  - **The render-log bot** enables auto-merge when main has a merge queue, instead of merging (a new case in
    `hack/validate-render-log-pr.py`). A queue entry made with `GITHUB_TOKEN` would start no `merge_group` run, so
    the App token (#356) comes first, then the ruleset.
  - [release.md](../release.md) has "How a pull request merges".
