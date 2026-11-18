---
type: Log
title: Render-log bot updates a queued pull request
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Render-log bot updates a queued pull request (#387).** Run 44291452478 found bot pull request #384 already in the
  merge queue, queued by the run before it. GitHub refused the push to its branch (GH006), and the run failed. The
  entry that run rendered then waited for the next docs/log.d merge, because the bot's own merge starts no run: the
  paths filter is the loop guard, and it is unchanged.
  - **The fix:** before pushing to an open bot pull request, `hack/render-log-pr.sh` asks GraphQL whether it is in the
    queue (`isInMergeQueue`). If it is, the script:
    - takes it out with `dequeuePullRequest`;
    - pushes;
    - waits for the required checks on the new commit;
    - enqueues it again.
  - **Queued during the run:** a push refused because the pull request entered the queue after the script looked
    re-reads the state and takes the same path once. A refused push to a pull request that isn't queued fails, and so
    does a queue state that can't be read.
  - **Tests:** `hack/validate-render-log-pr.py` adds three cases: already queued, queued mid-run, and refused while
    not queued. Its local remote refuses pushes to a queued branch the way GitHub does. Six mutants now run inside the
    gate, among them no dequeue and enqueue before the checks pass.
