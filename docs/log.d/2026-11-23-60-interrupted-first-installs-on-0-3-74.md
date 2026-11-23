---
type: Log
title: interrupted first installs and mid-upgrade kills on 0.3.74, and why four APIs failed
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **2.13.128's automatic reinstall is exercised on our charts (r074g, released 0.3.74, full-strict, 3-node GKE, 2026-11-23).**
  - **The kills:** the tacker and cyborg composition controllers were force-deleted while an install hook Job was active,
    with the release at v1 `pending-install` and later hooks not created.
  - **The result:** one `InterruptedInstallReinstalled` event each, one v1 deployed release, every hook Job Complete with a
    new UID, Ready, and no annotation left. A marker row in each database survived.
  - **Could the row have been lost?** Cyborg ran with `manifests.job_db_drop: true`, and an ordinary delete of its
    Composition then did drop the row. Tacker's hook is off by default, so its row proves nothing.
  - **What is lost:** tacker's 3 claims were replaced. core-provider's next release is to stop uninstalling.
  - **The event overstates:** "deleted 7 hook Job(s)" counts the hooks named in the release record (confirmed by
    core-provider from its code).
- **The control: a once-deployed tacker killed mid-upgrade was not reinstalled.** Helm's history is v1, v2
  `pending-upgrade`, v3 `Rollback to 1`, v4 deployed; claims and marker kept, hooks run again. The upgrade was started
  with `rerunHooks.tacker`, its first use at 0.3.74. The script reported FAIL on its event count, 1 to 0. The first event
  had expired. One 50 minutes old was still listed, and none was newer.
- **#459 diagnosed.** trove: #462. masakari and cloudkitty: #463. blazar, freezer and magnum were the probe's own requests.
- **The README says all of it**, with the stale `pending-upgrade` record 2.13.128 leaves behind.
