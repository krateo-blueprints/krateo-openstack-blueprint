---
type: Log
title: masakari and cloudkitty run the images built here
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **masakari and cloudkitty move to `ghcr.io/krateo-blueprints/images/{masakari,cloudkitty}:2026.1-ubuntu_noble-20261124`**
  (#463), published by the image-build run on b2852aa2 and pullable anonymously. Compared with upstream's 20261117 builds
  they add cryptography 43.0.3, cffi and pycparser, and a newer certifi; no package is lost, and user, entrypoint and PATH
  are the same.
- **The PyJWT exceptions move with them** (CVE-2026-102268): OpenStack's 2026.1 upper constraints pin 2.11.0, so a build under
  them carries it too. Same expiry, 2027-02-15.
- **The guard:** `hack/check-image-packages.py` runs with the image scan, which now lists every package. A chart that sets
  `memcache_security_strategy: ENCRYPT` may not run an image that has keystonemiddleware without cryptography.
- **Not run on a cluster yet:** the two images, and a token cached with ENCRYPT by them.
- **Kernel-header findings:** this branch's first scan found 7 new CVEs in `linux-libc-dev` in barbican's and the four gnocchi
  images (35 findings, no fix published), which this change doesn't touch. Diego's decision: rebuild those images without
  the package (#454). Until then they get dated entries to 2026-12-23.
