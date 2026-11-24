---
type: Log
title: the powerdns smoke test restarts an exited server as the kubelet would
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The failure:** the merge queue dropped #471, which doesn't touch powerdns, because the server "did not pass readiness after
  the outage with a NOTIFY queued". It had failed no earlier lint run among the last 60.
- **The test's gap:** it started the server again only if it had exited before the database was back. A server that exits
  in the seconds after that look (it exits when its communicator thread can't reach the database) failed the test at once,
  because the wait gives up when the server is gone. The kubelet restarts it. The failed log doesn't record the server's state, so that this is what happened isn't proven.
- **The change:** the 45 s wait restarts an exited server and counts it, and a failure now prints the restarts, the state and
  the last log lines. The limit and what must pass are unchanged.
