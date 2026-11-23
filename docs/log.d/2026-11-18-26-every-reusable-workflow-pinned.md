---
type: Log
title: Every reusable workflow pinned (rule 9)
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Every reusable workflow pinned (krateo-blueprints/charts rule 9, charts#20).** The three krateo-platformops/.github
  calls are pinned to `f25d6b5 # main 2026-11-17`: `lint-docs.yaml`, `release-notes.yaml` and `security.yml`. That
  repository has no tags yet, and `security.yml` runs here with `security-events: write`. `lint.yaml` gains a
  `pinned-uses` job, the shared check from charts 1.2.0 (`33c7951`). It refuses any non-local reusable-workflow `uses:`
  that is not a 40-hex commit with a `# X.Y.Z` or `# main YYYY-MM-DD` comment. `release-tag.yaml` keeps
  `788f1a6 # 1.1.0`, since the publisher and the preflight are byte-identical at 1.2.0. Run here, the check reports
  6 reusable uses, 0 not pinned. `pinned-uses / pinned-uses` becomes a required merge-queue check in
  `hack/merge-queue-ruleset.json`, and `hack/validate-merge-queue-ruleset.py` maps its inner job name. The live ruleset
  is updated after merge.
