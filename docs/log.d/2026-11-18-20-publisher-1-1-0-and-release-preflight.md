---
type: Log
title: Publisher 1.1.0 and the release preflight
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Publisher 1.1.0 and the release preflight.** `release-tag.yaml` pins krateo-blueprints/charts 1.1.0 (`788f1a6`).
  - **Publisher:** from 1.1.0 it refuses any version that is not plain `X.Y.Z` (its rule 13).
  - **Preflight (rule 12):** a new `preflight` job calls the `release-preflight.yaml` reusable, and `package-and-push`
    `needs:` it, so a refusal publishes nothing.
  - **The hook:** `hack/release-preflight` refuses a tag whose commit has no `X.Y.Z-main.g<sha7>` pre-release in GHCR.
    It also refuses when GHCR cannot be asked.
  - **Why the hook:** nothing else stopped a tag on a commit that skipped the pre-release channel, or a tag on a
    branch commit. Lint runs on every pull request, so a branch commit passed the lint check too.
  - **What it cannot prove:** that the GKE pair passed.
  - **Gate:** `hack/validate-prerelease-channel.py` checks the pins, the `needs:` chain and the hook's behaviour
    against a stubbed GHCR. All 45 mutations are caught. See [release.md](../release.md).
