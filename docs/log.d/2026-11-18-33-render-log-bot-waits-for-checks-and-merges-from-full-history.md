---
type: Log
title: Render-log bot waits for its checks and merges from a full history
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Render-log bot waits for its checks and merges from a full history (#379 follow-up).** Two failures on the bot's
  merge-queue path:
  - **Read "no checks yet" as a failure.** Run 44280066151 opened #384 at 09:53:05Z and gave up a second later with
    "failed a required check". `gh pr checks --required --watch --fail-fast` exits non-zero before any check exists on
    a new head.
    - The bot now polls the check runs of exactly the commit it pushed, against the ruleset's own required list.
    - A check not reported yet is waited for, up to 10 minutes to appear and 60 to finish, and then the bot fails
      saying so.
    - A failed required check stops it at once. A non-required check is ignored.
    - A ruleset that requires nothing is refused rather than enqueued unchecked.
  - **Refused to update an open bot pull request.** Run 44281145388 failed with "refusing to merge unrelated
    histories". `render-log.yaml` checked out main at depth 1, so main's commit had no parents.
    - The checkout now uses `fetch-depth: 0`.
    - `render-log-pr.sh` stops if the merge does not start at all, instead of swallowing the error with `|| true`.
  - **Gate:** `hack/validate-render-log-pr.py` gains cases for each state:
    - a check reported late, then passing;
    - a check never reported;
    - a check reported but never finished;
    - a failed check;
    - an empty required set;
    - the update path from an actions/checkout-style clone. At depth 1 it reproduces the refusal; at the workflow's
      depth it merges.

    Six mutants are caught, including the depth-1 workflow and the original "not reported means failed" reading.
