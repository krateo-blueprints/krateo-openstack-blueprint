---
type: Log
title: The pull-request gates measure from an explicit base
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The pull-request gates measure from an explicit base.**
  - **What was wrong:** `hack/validate-log-untouched.py` and `hack/validate-no-skip-markers.py` inferred where a pull
    request's change starts from the shape of HEAD. A two-parent HEAD was taken for GitHub's merge ref and measured
    from its first parent. A branch head that has merged main is also two-parent, but with the branch as its first
    parent, so main's merged commits were charged to the branch. Run on #412's head, the log gate failed for main's
    render of `docs/log.md`, which the branch never touched. The skip-marker gate would have read an old render's skip
    marker the same way.
  - **The fix:** both gates measure from the merge base of HEAD and an explicit base, through one helper,
    `hack/pr_range.py`.
    - **pull_request:** the base is `origin/$GITHUB_BASE_REF`. That is exact for the merge ref, the pull request's
      head, and a head that merged main.
    - **workflow_dispatch:** the base is `origin/main`, declared.
    - **By hand:** `--base <ref>` is required, or `LOG_BASE=<ref>` for a run through `hack/run-gates.py`, which
      passes a gate no arguments. A pull_request run ignores `LOG_BASE`. With neither, the gates fail rather than
      guess. A local `run-gates.py --job job-hooks` also needs that job's workflow-env variables to exist; with them
      set empty and `LOG_BASE=origin/main`, all 26 of its gates pass.
    - **merge_group and push** keep their fixed BASE (`merge_group.base_sha`, or the previous head). A missing BASE
      now fails, where it used to fall back to `origin/main`.
  - **Tests:** throwaway repositories cover a pull_request merge ref, the pull request's head, a branch head that
    merged main, and a merge group. Each case runs with `docs/log.md` left alone and with it edited (log gate), or
    with and without a skip marker (skip-marker gate). Restoring the old parent-count inference fails the
    merged-branch cases in both gates. Four base cases: neither given is refused, `LOG_BASE` alone, `--base` over
    `LOG_BASE`, and a pull_request ignoring `LOG_BASE`.
