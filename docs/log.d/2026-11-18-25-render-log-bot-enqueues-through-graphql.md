---
type: Log
title: The render-log bot enqueues through enqueuePullRequest
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The render-log bot enqueues through enqueuePullRequest.** The merge-queue ruleset is applied (2026-11-18), and the
  bot's queue step would have failed on its next render. It ran `gh pr merge --auto --squash`, and gh (2.86) does
  that through `enablePullRequestAutoMerge`, as it does `gh pr merge` without `--auto`. This repository has
  auto-merge disabled, so the call fails with "Auto merge is not allowed for this repository". The queue is the
  mechanism, so auto-merge stays off.
  - **Change:** under a merge queue, `hack/render-log-pr.sh` now waits for its pull request's required checks
    (`gh pr checks --required --watch`). It then adds the pull request to the queue with the GraphQL
    `enqueuePullRequest` mutation, using the pull request's node id and the App token. It never runs `gh pr merge`.
    If a required check fails, nothing is enqueued.
  - **Tests:** `hack/validate-render-log-pr.py` case 6 now requires the checks wait, then one enqueue mutation with
    the node id, and no `gh pr merge`. A new case 7 covers a failed check: nothing is enqueued or merged. A mutant
    that brings back `--auto` is caught.
