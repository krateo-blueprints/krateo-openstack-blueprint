---
type: Log
title: No shell line pipes a producer into grep -q
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **No shell line pipes a producer into `grep -q`.** Under `set -o pipefail`, `producer | grep -q pattern` fails
  when grep exits on its match while the producer still has output to write: the producer takes SIGPIPE (rc 141)
  and pipefail makes that the pipeline's status, so a line that was found reads as missing.
  - **Where it came from:** nova-kog's 0.1.9 release refused its own chart this way (#183, 2026-11-18): the file it
    looked for was the third of 24 archive entries, so tar still had most of its listing to write.
  - **Here:** one instance, `hack/smoke-migrate-to-pvc.sh`'s second `migrate prepare`. It now captures the output,
    then matches with `<<<`, and a failing prepare fails as such rather than as "did not recognise".
  - **Same class, other readers:** `| head` (the GHCR push script's `tar -tzf | head -n1` died 20 of 20 times on a
    20,001-entry archive) and `| grep -m N`. The guard refuses all three, and refuses a small `printf | grep -q`
    deliberately: it is safe only while the output fits one pipe write, which a scan cannot know.
  - **The guard:** `hack/validate-no-pipe-into-grep-q.py` (job-hooks) scans every workflow and shell file. It fails
    on scanning nothing, and its self-test must catch four planted forms and pass four safe ones.
