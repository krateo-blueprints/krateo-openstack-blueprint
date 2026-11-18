---
type: Log
title: trove's db-purge CronJob removed (#289)
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **trove's db-purge CronJob removed (#289).** It rendered by default and ran `trove-manage db_purge --age_in_days 30`.
  trove-manage on stable/2026.1 has no `db_purge`: `trove/cmd/manage.py` registers 12 commands, none a purge, and no
  file in trove's tree defines a database purge. Every scheduled run would have failed at argument parsing, and a
  failing CronJob, unlike a failing hook, blocks nothing that anyone sees. With no command to point it at, the CronJob,
  its script and values are gone. `manifests.cron_job_db_purge: true` is refused by the schema and by
  `templates/removed-values.yaml`; `false` is still accepted for older Compositions (`hack/validate-removed-values.sh`
  covers both). New gate `hack/validate-cron-commands.py`: every `<project>-*` program that a CronJob's script starts,
  in every chart and whether or not the CronJob is on by default, must be given a command listed for it in
  `hack/cron-program-commands.yaml`, measured from the program's stable/2026.1 source. It is static, since the command
  is in the script text. It found no other CronJob running a missing command (15 CronJobs, 20 commands).
