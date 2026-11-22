---
type: Log
title: the MariaDB server restarts only for the scripts it mounts
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **A controller-only change restarted the database (#445, found by r074 on the 0.3.71 -> 0.3.74-main.gc16973f
  upgrade).**
  - **Measured:** the server StatefulSet got a new revision at 18:34:26Z, the one MariaDB pod was replaced at
    18:34:48Z, and Keystone's token API failed from 18:34:23Z to 18:36:18Z. The upgrade's Keystone-user Jobs retried
    until it was back.
  - **Cause, two changes (a field-by-field render diff, 0.3.71 against this release):**
    - the server's pod template carried upstream's `configmap-bin-hash`, a hash of the whole rendered `mariadb-bin`.
      That ConfigMap also holds `mariadb_controller.py`, which only the controller Deployment runs, and #335 added its
      readiness heartbeat. This is #445;
    - the pod template also carries hashes of the credential ExternalSecret manifests (dbadmin, sst, dbaudit), which
      #422 moved to `refreshPolicy: OnChange`. That part is not changed here: a new root must still roll the server.
    - RabbitMQ rolled at the same upgrade for the same #422 reason (admin and Erlang-cookie ExternalSecrets) and for
      #343's dated init-container image.
- **The fix:** `templates/_bin_hash.tpl` hashes, per workload, only the `mariadb-bin` keys it mounts, read back from
  the rendered ConfigMap itself. The server hashes `start.py`, `stop.sh` and `health.sh`, and the controller
  `mariadb_controller.py`. Upstream divergence `mariadb-445`.
- **The guard:** `hack/validate-mariadb-bin-hash.py`, in job-hooks. It appends a line to every `templates/bin/` source
  of a rendered key, one at a time, with defaults and with backup on. Every long-running workload mounting
  `mariadb-bin` must change its pod template exactly when a key it mounts changed, and each mutation must reach the
  ConfigMap. The planted control restores upstream's shared hash on the server and must be caught on the controller's
  script.
- **Upgrading:** the first upgrade to 0.3.74 restarts the MariaDB and RabbitMQ servers once each, on every install, for
  the reasons above; the new annotation value is part of that one restart. Later changes to scripts the server does not
  mount no longer restart it (README "Upgrading to 0.3.74 or later: MariaDB and RabbitMQ restart once").
