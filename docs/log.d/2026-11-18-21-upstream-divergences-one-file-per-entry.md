---
type: Log
title: Upstream divergences, one file per entry
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Upstream divergences, one file per entry.** Every change that diverged from upstream appended to
  `hack/upstream-divergences.yaml`, so each merge re-conflicted the branches in flight (#352, #353, #355, #344 and
  #357 on 2026-11-18). Each divergence is now `hack/upstream-divergences.d/<component>-<slug>.yaml`, holding one
  entry. The 19 entries were split out with their text and the comments above them, including #344's three. Loaded
  from the directory, they are the same 19 entries and the same 81 assertions as the old file at 9c177be. The old file is removed.
  - **Refusals:** `hack/validate-upstream-divergences.py` reads the directory and refuses, before checking anything:
    - a file that isn't exactly one mapping of `component`, `issue`, `reason` and `assertions` (two entries, a list,
      or a second YAML document);
    - a duplicate key;
    - a file whose name doesn't start with its component;
    - an assertion already declared in another file;
    - an empty directory;
    - the old file coming back.
  - [release.md](../release.md) says how to add one.
  - The case lists of `hack/validate-removed-values.sh` stay as they are for now.
