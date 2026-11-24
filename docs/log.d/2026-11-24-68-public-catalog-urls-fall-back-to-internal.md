---
type: Log
title: with publicEndpoints off, the public and admin catalog URLs are the internal ones
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The defect (#451):** with `publicEndpoints` off, none of the 22 public catalog endpoints was served on 0.3.74. 15 named a
  Service never created, 5 one that selects no pod, 2 a port the Service lacks. The admin URLs of keystone and cyborg (#326)
  had the same fault. An earlier check called 5 of them fine, because a Service of that name exists.
- **Diego's decision (2026-11-23):** public falls back to internal.
- **The fix is in one place:** helm-toolkit's `keystone_endpoint_uri_lookup` builds every catalog URL, and the URLs a chart
  writes for another service. A `public` or `admin` endpoint without an address of its own is now built as `internal`. An
  address is `host_fqdn_override` (a string, or a map with a host), an IP, or a map host. `publicEndpoints` sets the first, so
  nothing changes when it is on.
- **Ingress objects off by default in the eight charts that still rendered them.** Their Services selected pods that don't exist.
- **The guard:** `hack/validate-catalog-endpoints.py` renders every chart and follows each registered URL to a rendered Service, its
  port, and a pod its selector and targetPort reach. 0 failures here, 32 on main at 70b42c8f without this change; 10 cases of the rule.
- **Upgrades:** the endpoint hooks and keystone's bootstrap (2026.1) update an existing URL. Not run on a cluster yet.
