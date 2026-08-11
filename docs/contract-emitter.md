---
type: Architecture
title: krateo-openstack-blueprint — contract emitter
description: The runtime side of the serviceContract usage/health declaration — CronJobs that poll the OpenStack APIs on the declared intervals and write normalized UsageRecord / health rows to ClickHouse.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack
tags: [openstack, contract-emitter, usage, health, clickhouse]
timestamp: 2026-08-11T00:00:00Z
---

# Contract emitter — runtime usage & health emission

The umbrella chart's `serviceContract` stanza *declares* what this service
emits (`serviceContract.usage.metrics`, `serviceContract.health`). The
**contract emitter** is the runtime side of that declaration: two CronJobs
that poll the OpenStack APIs on the declared intervals and write normalized
rows to ClickHouse over its HTTP interface. Together with the descriptor
ConfigMap, the capability ClusterRoles and the SSO deep-link template, it
makes this blueprint a complete reference implementation of the service
integration contract.

Everything is chart-level and vendor-generic: a stdlib-only Python script
(`blueprints/openstack/chart/files/contract-emitter.py`) on a plain
`python:3-alpine` image, configured entirely from values. No controller, no
CRD, no platform-specific code.

## Enabling

Disabled by default (the chart must stay installable without a ClickHouse
data plane). The management platform enables it and points it at its own:

```yaml
contractEmitter:
  enabled: true
  org: my-org                       # stamped on every emitted record
  clickhouse:
    url: http://clickhouse.krateo-system.svc.cluster.local:8123
    database: krateo
```

OpenStack credentials come from the OpenStack-Helm admin openrc secret the
keystone component already creates (`keystone-keystone-admin`, keys
`OS_AUTH_URL`, `OS_USERNAME`, `OS_PASSWORD`, ...), so there is nothing to
wire on a default install.

## Usage records (`MODE=usage`)

One CronJob on `serviceContract.usage.interval`. Rows land in
`<database>.usage_records` (the normalized *UsageRecord* shape consumed
uniformly by dashboards and rating engines):

| column | value |
|---|---|
| `record_id` | showback's `deterministicID(org, tenant, service, resource, metric, window)` — sha256 over NUL-terminated parts, first 32 hex chars. Re-runs in the same window replace, never duplicate, and a direct insert carries the same id showback would compute on API ingest |
| `org` | `contractEmitter.org` |
| `tenant` | Keystone project name (empty for org-level capacity) |
| `service` | `serviceContract.service.name` |
| `resource_id` | project id / `hypervisors` / `cinder-pools` |
| `metric`, `quantity`, `unit` | as declared in `serviceContract.usage.metrics` |
| `window_start`, `window_end` | interval-aligned UTC window |
| `tags` | the Keystone **project tags**, split on the first `:` into real key→value Map entries (`env:prod` → `{env: prod}`) so per-tag `GROUP BY` works; a bare tag without `:` falls back to `{tag: ""}` |
| `source` | `openstack-blueprint` |

Collectors (all best-effort — a missing service skips its metrics):

- **Nova** `GET /limits?tenant_id=` → `instances.count`, `vcpu.used`, `ram.used`
- **Nova** `GET /os-hypervisors/statistics` → `vcpu.capacity`, `ram.capacity`
- **Cinder** `GET /os-quota-sets/<id>?usage=True` → `storage.used`
- **Cinder** `GET /scheduler-stats/get_pools` → `storage.capacity`
- **Neutron** `GET /v2.0/floatingips` → `floating_ips.count`

## Health records (`MODE=health`)

One CronJob on `serviceContract.health.interval`. Every service in the
Keystone catalog is probed and mapped onto **exactly**
`OK | Warning | Critical | Unknown`:

- `2xx/3xx/401/403` → `OK` (the API answers; auth-required is healthy)
- `5xx` / connection error → `Critical`
- other `4xx` → `Warning`
- timeout → `Unknown`

A consolidated `_service` row (worst component wins) gives aggregators a
single per-service signal. Rows land in `<database>.health_records`
(`CREATE TABLE IF NOT EXISTS` bootstrap is on by default,
`clickhouse.bootstrapHealthTable`).

## Conformance test

`tests/test_contract_emitter.py` (stdlib `unittest`, fully offline) runs
`collect_usage`/`collect_health` against mocked OpenStack API responses and
asserts row shape against the showback `usage_records` schema, the
showback-aligned deterministic `record_id`, `key:value` tag splitting and
the exact health enum — including the worst-wins `_service` consolidation
and same-window idempotency. It runs on every PR via
`.github/workflows/lint.yaml`:

```console
$ python3 -m unittest discover -s tests -v
```

## SSO deep-link resolution

The descriptor's `serviceContract.sso.deepLink.urlTemplate` (Skyline console,
`{project}` placeholder, Keystone token-rescope pre-redirect) is what a portal
resolves per caller context. `tools/resolve-deeplink.sh` performs the same
substitution stand-alone — used by CI to assert the template resolves, and
with `--check` to probe a live console:

```console
$ tools/resolve-deeplink.sh --from-chart --project demo
https://skyline.openstack.example.com/demo
```
