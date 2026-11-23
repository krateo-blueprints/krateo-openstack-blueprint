---
type: Log
title: Charts pushed through the shared chart-push
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Charts pushed through the shared chart-push (charts rule 14).** `hack/publish-charts.sh` pushed each chart with
  `helm push` in a blind three-attempt retry loop. charts 1.4.0's rule-14 check scans only workflow files, so it missed
  this.
  - **The push:** `release-tag.yaml` and `prerelease.yaml` now locate the shared push.sh with a `chart-push` step,
    pinned to `krateo-platformops/.github` 1.0.0 (`e1d3e99`).
    - That chart-push directory is byte-identical to `653ac1e`'s, and its push.sh's sha256 is `e2912643…7b03`.
    - They pass `CHART_PUSH` and the registry credentials to the publish step.
  - **The script:** it pushes each chart once with `bash "$CHART_PUSH"`, and a failure stops the release.
    - push.sh retries only GHCR's tag race, and only after the registry serves the digest.
    - The script refuses to stamp anything when `CHART_PUSH` is unset.
  - **The gate:** `hack/validate-prerelease-channel.py` found release-tag's push step by the text `helm push`, which
    the migration removed. It now finds the step by its `hack/publish-charts.sh <channel>` invocation, with shell
    comments stripped, and fails when there is none. It also checks the pinned `chart-push` step, the publish step's
    `CHART_PUSH` and credentials, and that no step runs `helm push`.
    - A stand-in run checks that `$CHART_PUSH` is called once per chart, that one failure is not retried, and that a
      missing `CHART_PUSH` stamps nothing.
    - 59 mutants are caught, among them the push step removed and the push step reduced to a comment.
