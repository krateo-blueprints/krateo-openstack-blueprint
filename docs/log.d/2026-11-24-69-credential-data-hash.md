---
type: Log
title: MariaDB and RabbitMQ restart for credential data only
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The defect (#449, r074):** both one-replica servers' pod templates hashed their credential manifests whole. #422 changed only
  the refresh policy of the ExternalSecrets, and the 0.3.71 to 0.3.74 upgrade restarted both: MariaDB down 70 s, RabbitMQ 40 s,
  Keystone's tokens failing for 2 minutes.
- **The fix:** `helm-toolkit.utils.secret_data_hash`. For a Secret it hashes name, type and data; for an ExternalSecret the Secret
  it writes, where the values come from and how they are put together; for any other kind the whole object. mariadb's three
  password hashes and its exporter's, and rabbitmq's admin and cookie hashes, use it.
- **The guard:** `hack/validate-credential-hash.py` renders both charts as the umbrella delivers them. A changed refresh policy,
  label, annotation and creation policy must leave the pod template identical; another ESO template, another key or another
  root must change it; the old hash is planted on every run and must be caught. 13 comparisons.
- **This release still restarts both servers once**, because the annotation values change. Not run on a cluster yet.
