---
type: Log
title: process counts no longer come from the node's CPUs, and gnocchi-api recycles its workers
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Found by the 0.3.75 candidate run (main fd30c0ff, GKE, 3 x n2-standard-16, core-provider 2.13.129, 2026-11-24).** #324's memory limits
  met programs that start one process per node CPU. cloudkitty-processor (16 + 16 workers) was OOM-killed in 5 s at 1 GiB and its
  component never became Ready. watcher-api used 713 of 1024 MiB and skyline 1152 of 2048 MiB: on 32 CPUs they would follow. No
  release has these limits yet.
- **The fix:** fixed counts, each measured on that cluster: cloudkitty 1 + 1 (232 MiB), watcher-api 2 (123), magnum-conductor 2 (135),
  skyline 2 (332).
- **gnocchi-api** was OOM-killed at 1 GiB within a minute of cloudkitty's first queries. gnocchi 4.7.0 keeps about 0.47 MiB of every
  aggregates query with `use_history`. Upstream's commit that mentions a leak there (5d3b5494, in no release) changes nothing:
  +191 MiB against +188 over 400 queries. So uWSGI now replaces a worker, between requests, above 200 MiB: at most 567 MiB for both
  over 2,400 queries, 15 replacements, none lost. Two earlier readings of mine were wrong and are corrected here: the first
  (0.24 MiB per query) was mostly warm-up, and it used queries that don't reach the leak.
- **The guard:** `hack/validate-worker-counts.py`: the four options are rendered as integers, nothing rendered asks the machine for its
  CPUs, and the recycling bound fits the limit. 6 findings on main at fd30c0ff.
