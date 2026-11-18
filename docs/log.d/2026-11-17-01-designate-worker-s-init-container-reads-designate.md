---
type: Log
title: designate-worker's init container reads designate.conf.d (0.3.70, #291)
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **designate-worker's init container reads designate.conf.d (0.3.70, #291).** The 0.3.69 GKE upgrade never got
  designate Ready: `designate-worker-init` runs `designate-manage pool update`, which reaches designate-central over RPC,
  and it mounted `designate.conf` alone. 0.3.69 had moved `transport_url` into `designate.conf.d`, so the `[ -d ]` guard
  dropped `--config-dir` and oslo.messaging dialled localhost:5672 forever. The init container now mounts the pod's own
  drop-in volume, as the worker does. The gate had left init containers out on a premise that was false for this chart
  (that they share their pod's drop-in volume): checks 13, 22 and 22m now treat an init container that starts an
  OpenStack program as a reader, and new check 19c requires every such init container, in every chart and rendered mode,
  that mounts its project's main config to mount its `.conf.d` too. The survey found no other one.
