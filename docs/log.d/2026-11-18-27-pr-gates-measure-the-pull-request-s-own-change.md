---
type: Log
title: The pull-request gates measure the pull request's own change
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The pull-request gates measure the pull request's own change.** `hack/validate-log-untouched.py` diffed
  `BASE..HEAD` on a pull request, with BASE the pull request's `base.sha`. That can be older than main's tip, so any
  bot render of `docs/log.md` that landed after the branch forked counted as the branch's change. #376 at 8d5240e
  failed this way without touching the file. `hack/validate-no-skip-markers.py` read commits over the same range, so
  the skip markers in the old bot renders' messages would have failed a stale branch too.
  - **Fix:** on a pull request, both gates now measure from the first parent of the merge ref that actions/checkout
    checks out (`refs/pull/<n>/merge`, main's tip as GitHub merged it). When HEAD isn't a merge commit, they measure
    from the merge base of BASE and HEAD instead. The merge base alone isn't enough on the merge ref: there it is
    BASE itself, and main's later commits still count.
  - **Unchanged:** a merge group's range (`merge_group.base_sha` is fixed, and the group's commits sit on top of it)
    and a push's range (the previous head) were already right.
  - **Tests:** each gate now builds throwaway repositories in which main gains a commit after the fork:
    - log-untouched: a branch that only adds a log.d entry passes, and one that edits `docs/log.md` still fails, both
      on the merge ref and at the head;
    - skip-markers: a marker in main's commit isn't read, and the branch's own is.

    Mutants caught: two-dot restored in either gate, and the merge base alone in log-untouched.
