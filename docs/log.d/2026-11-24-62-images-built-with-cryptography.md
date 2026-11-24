---
type: Log
title: masakari and cloudkitty images built here with the cryptography package
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The defect (#463, r074g, 0.3.74 on GKE, 2026-11-23):** masakari answers 500 to every request with a valid token and
  cloudkitty closes the connection. Their charts set `memcache_security_strategy: ENCRYPT`; their images lack `cryptography`
  (`CryptoUnavailableError`), in the pinned 20261117 builds and in 20261123's. With `MAC` instead, both answered 200, so the
  missing package is the whole cause.
- **Diego's decision:** build the two images here, from upstream's Dockerfiles, with their own `EXTRA_PYTHON_PACKAGES` argument
  set to `keystonemiddleware[memcache_encryption]`. Encryption of the token cache stays on.
- **`hack/image-build.yaml`** pins the Dockerfiles' commit, the two base images by digest and the source commits. The
  Dockerfile clones a branch, so the build refuses to run when the branch has moved, and checks the commit the image
  records.
- **`.github/workflows/image-build.yaml`** builds and verifies on a pull request with no write permission, and publishes on main
  only, to dated tags it never moves.
- **This change only builds the images.** The charts move to them in the next one, once the tags exist and the scan has
  seen them.
