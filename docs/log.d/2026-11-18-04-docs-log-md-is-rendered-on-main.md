---
type: Log
title: docs/log.md is rendered on main, by a bot pull request
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **docs/log.md is rendered on main, by a bot pull request.** After #340, every branch still committed a regenerated
  `docs/log.md`, so merges kept conflicting on it (#322, #320).
  - **Pull requests:** a pull request now adds its `docs/log.d` entry and nothing else.
    `hack/validate-log-untouched.py` fails one whose diff touches `docs/log.md`. It makes one exception, for the
    bot's own pull request, and only when the branch is `bot/render-log-*` and the author is `github-actions[bot]`.
    The rule is checked case by case first (prefix with a human author, bot author on another branch, a look-alike
    prefix, another bot). `hack/render-log.py --check` now validates only the entries: names with real dates, front
    matter, the bullet, and links that resolve from `docs/log.d`.
  - **Main:** after each merge, `.github/workflows/render-log.yaml` re-renders `docs/log.md`. If it changed,
    `hack/render-log-pr.sh` commits it as `github-actions[bot]` on `bot/render-log-<sha7>` with `[skip ci]`, opens a
    pull request and squash-merges it. If a bot pull request is already open, it updates that one with a
    fast-forward instead. It runs one at a time and commits nothing when the render is unchanged. It's a pull request
    rather than a push because main's org ruleset requires one and has no bypass actor (Diego, 2026-11-18). It needs
    "Allow GitHub Actions to create and approve pull requests", which is off for the org and the repo (checked
    2026-11-18).
  - **Gate:** `hack/validate-render-log-pr.py` runs the script against a local remote and a stand-in `gh`, in four
    cases: current, a new entry, a bot pull request already open, and after landing.
