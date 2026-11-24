---
type: Log
title: barbican's and gnocchi's images rebuilt without the kernel headers
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The problem (#454):** barbican's image and the four Kolla gnocchi images carry `linux-libc-dev`. Every kernel CVE is a
  finding in it with no fix published: 15 new CRITICAL findings on 2026-11-23, 35 on the 24th, each blocking image changes
  and release tags. It is header files; nothing in the images runs it.
- **Diego's decision:** remove the package instead of excepting it batch by batch.
- **`hack/image-build.yaml` gains a second kind of image:** upstream's own build, at the digest the scan measured, minus an exact
  list of Debian packages. `dpkg --purge` has no network and no resolver, and refuses while anything else depends on a listed
  package. The result must have upstream's Debian packages minus the list and the same Python packages, still import its
  service, and keep user, entrypoint and PATH.
- **Gnocchi's four are built from our mirror:** quay no longer serves the recorded digests, because Kolla rebuilds its series
  tags in place. `hack/image-mirror.yaml` holds the same digests.
- **What goes:** `linux-libc-dev` and what depends on it, all compile-time (`libc6-dev`, the `-dev` packages, g++ in gnocchi's):
  8 packages of 211 in barbican, 17 of 290 in gnocchi's. The lists come from the scan's own package data.
- **This change only builds them.** The charts move in the next one, and the dated exceptions go with it.
