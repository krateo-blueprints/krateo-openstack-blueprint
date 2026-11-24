---
type: Log
title: barbican and gnocchi run the images rebuilt without the kernel headers
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **barbican and gnocchi move to `ghcr.io/krateo-blueprints/images/`** (#454): upstream's builds minus compile-time Debian
  packages, published by the image-build run on 4e06df70.
- **All 75 `linux-libc-dev` exceptions are gone.** The five PyJWT ones move with the images, same expiry: no Python package
  changed.
- **gnocchi is pinned for the first time (#334).** It ran Kolla's series tags, which are rebuilt in place; the 17 November build
  we had recorded was no longer on quay. The chart now runs that build, from our mirror, under a dated tag, so the four pin
  waivers go. This moves gnocchi back from whatever Kolla had rebuilt since to the recorded build.
- **The mirror list is empty:** nothing it lists is run any more. Its tags stay published.
- **Not run on a cluster yet.**
