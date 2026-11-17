---
type: Log
title: One file per log entry and per gate
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **One file per log entry and per gate.** Every pull request edited the top of `docs/log.md` and the step list
  of `.github/workflows/lint.yaml` in the same places, so branches in flight collided on both (Diego, 2026-11-17).
  - **Log:** each entry is now `docs/log.d/<yyyy-mm-dd>-<nn>-<slug>.md` with OKF front matter, and
    `hack/render-log.py` writes `docs/log.md`, which stays checked in. The 14 existing entries were split out in their
    order, dated from the commit that added each. The rendered file differs from the old one only by two blank lines
    between entries. `hack/render-log.py --check` fails when `docs/log.md` is stale.
  - **Gates:** the 76 check steps became `hack/gates.d/<gate>.yaml`, one file each, with name, job, order, the exact
    command and its comment. Each of the 11 jobs keeps its tools and runs its gates through one step,
    `hack/run-gates.py --job <job>`. The two GitHub expressions (the job-hooks base commit, the migrate-to-pvc
    matrix) come in through the runner step's `env`. The commands CI runs, listed per job before and after, are the
    same and in the same order. The only additions are the two new gates. `hack/validate-gates-registered.py` fails
    when a `hack/validate-*` script is in no gate file, or a gate file names a script that is missing.
  - [release.md](../release.md) says how to add both. `docs/log.md` and the check steps of `lint.yaml` are never
    edited by hand.
