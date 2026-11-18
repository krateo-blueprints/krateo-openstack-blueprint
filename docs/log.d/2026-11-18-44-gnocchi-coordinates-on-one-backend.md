---
type: Log
title: gnocchi coordinates on one backend
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **gnocchi was never Ready with ha.enabled (#309 H2, run 4).** On GKE, 0.3.74-main.g0e23f7a passed every messaging
  predicate of p0, but gnocchi-api failed readiness: `coordination backend memcached-discovery://memcached-ha-0…:11211,
  memcached-ha-1…,memcached-ha-2… is unusable: NoMatches: No 'tooz.backends' driver found, looking for
  'memcached-discovery'`. metricd, statsd and ceilometer waited behind it, and the umbrella never reported Ready.
  - **The cause:** upstream's `configmap-etc.yaml` took the tooz driver from `endpoints.oslo_cache.hosts.default`,
    which is right only while that host is literally `memcached`. H0/H1's `osh.haValues` (#344) sets it to the headless
    Service `memcached-discovery`, and the statefulset turns the URL into a host list.
  - **Wrong in principle too:** sack locks spread over memcached shards would let two metricd hold one sack.
  - **The fix:** with `storage.driver: redis` (the default), `coordination_url` is left unset, so gnocchi coordinates
    on the metric store's redis (`gnocchi/service.py prepare_service`). Any other storage driver gets a literal
    `memcached://` on one host, and a memcache host list is refused.
  - **A behaviour change without HA as well:** coordination moves from memcached to redis. Redis is already gnocchi's
    storage and incoming store, so this adds no new failure mode. That redis is a single replica, and in the ha
    profile too; that is #411.
- **The guard, in `validate-ha-propagation.py` part 3:**
  - every `coordination_url` and `backend_url` rendered with the ha spec must name a driver tooz 8.1.0 registers
    (2026.1 upper-constraints) and exactly one backend;
  - gnocchi's effective lock backend (its coordination_url, else its redis_url) must exist;
  - two controls: run 4's URL, planted, must be refused, and gnocchi with a non-redis store under the ha spec must
    refuse to render.
  - **Why it was missed:** before, the gate counted run 4's URL as a valid memcache list, because it compared only the
    hosts. The divergence entry `gnocchi-309-coordination-backend` asserts three lines, each failing on upstream's
    template.
