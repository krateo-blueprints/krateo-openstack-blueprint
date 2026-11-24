---
type: Log
title: every install hook can run again, and a gate keeps it so
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Why (#457):** core-provider installs an interrupted first install again, so every post-install hook runs a second time over what
  the first run left (core-provider#629; exercised on tacker and cyborg, r074g). Every upgrade runs them again too.
- **`hack/validate-hooks-rerunnable.py`** judges the program of every container of the 178 install-hook Jobs of 37 charts:
  - no `openstack create` without a lookup of that noun or `--or-show`, no delete without a written reason, no unknown verb;
  - no SQL DROP, TRUNCATE or DELETE, and no CREATE without IF NOT EXISTS;
  - no `rabbitmqctl add_user` or `add_vhost`, no untested Ceph create, no `rm -r` of anything but a temporary directory;
  - the three Python programs are named, with how each survives a second run and the text that makes it so.
- **What it found:** nothing to fix. Its first run flagged seven scripts that `openstack_count` (#295) already guards, or that only
  mention SQL; each was read.
- **Not judged:** migration programs, which every upgrade runs over an existing database.
- **Known limit:** powerdns's schema has no transaction, so a db-sync pod killed in the moment it applies it leaves a partial schema,
  which the script refuses by design rather than guess.
