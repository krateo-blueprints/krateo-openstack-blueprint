---
type: Log
title: mariadb's remote backup and its ks-user Job removed (#302)
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **mariadb's remote backup and its ks-user Job removed (#302).** Turning on `manifests.job_ks_user` deadlocked a
  first install. The Job creates the Keystone user that Swift/RGW remote backups authenticate as. It is a
  post-install hook that waits on keystone (#301), and keystone waits on mariadb. The user it created also had role
  admin and OpenStack-Helm's published password. Diego decided (2026-11-17) to remove the path rather than move it.
  - **Removed:** the ks-user Job and script, the RGW Secret, `endpoints.identity` with `mariadb-backup-user`,
    `conf.backup.remote_backup`, and the backup CronJob's remote environment and Keystone credentials. The CronJob's
    wait on `mariadb-ks-user` is gone too.
  - **Refused:** `manifests.job_ks_user: true` and any `conf.backup.remote_backup` other than `enabled: false`, by the
    schema and by `templates/removed-values.yaml` when the schema is skipped. `false` stays accepted for older
    Compositions.
  - **Local backups are unaffected.** With `manifests.cron_job_mariadb_backup` and `conf.backup.enabled` on, the
    CronJob renders with `REMOTE_BACKUP_ENABLED=false` and waits only on mariadb.
  - **Gates:**
    - `hack/validate-removed-values.sh` gains nine mariadb cases.
    - `hack/validate-readiness-deadlock.py` now also renders every chart with every `manifests.job_*` switch on. It
      fails on a cycle that appears only with an opt-in Job on, which is how the ks-user Job got past it.
    - An upstream-divergences entry keeps the script, the user and the remote settings out across a re-vendor.
