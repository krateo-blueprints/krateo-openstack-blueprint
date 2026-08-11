---
type: Configuration
title: krateo-openstack-blueprint — configuration
description: The whole Openstack orchestrator value surface — profile, enabled, installDefinitions, ociRepo/chartVersion, the strictly-typed componentValues, the serviceContract stanza, and the contractEmitter CronJobs.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack
tags: [openstack, configuration, values, service-contract, contract-emitter]
timestamp: 2026-08-11T00:00:00Z
---

# Configuration

Everything below is the `spec` of an `Openstack` Composition (Kind `Openstack`,
`composition.krateo.io/v0-2-0`), fully typed by the orchestrator chart's
`blueprints/openstack/chart/values.schema.json`. The per-component blueprints have their
own, smaller schemas; the orchestrator's `componentValues` mirrors them.

## Orchestrator top-level

| Field | Type | Default | Purpose |
| --- | --- | --- | --- |
| `profile` | string (`identity` \| `full`) | `identity` | `identity` = MariaDB+Memcached+Keystone+Glance+Horizon; `full` = also RabbitMQ+Placement+OVS+libvirt+Neutron+Nova (compute; needs an amd64 cluster with the OVS kernel module). |
| `enabled` | array of string \| null | `[]` | Deploy exactly these components plus their transitive dependencies, **overriding** `profile`. E.g. `["heat"]` also brings up keystone, rabbitmq, mariadb and memcached. Empty uses `profile`. |
| `installDefinitions` | boolean | `true` | Register each component blueprint's `CompositionDefinition` as the first step. |
| `ociRepo` | string | `oci://ghcr.io/krateo-blueprints/charts` | Registry path holding the per-component blueprint charts. |
| `chartVersion` | string | `0.3.2` | Component chart version the orchestrator pins. |
| `componentValues` | object \| null | `null` | Per-component Composition `spec` overrides (see below). |
| `serviceContract` | object | chart default | Service integration descriptor, contract v1 (see below). |
| `contractEmitter` | object | disabled | Runtime usage/health emission CronJobs (see below). |

## `componentValues.<name>` — per-component overrides

Per-component Composition `spec` overrides, **strictly typed** against each component's chart
schema (regenerated per release by `hack/gen-componentvalues-schema.py`; CI fails a stale
schema). The value at `componentValues.<name>` becomes that component's Composition `spec`,
deep-merged; components without an entry use chart defaults. `additionalProperties: false` —
an unknown component or field is rejected.

Common shapes across components:

- `images.pull_policy` — `Always` \| `IfNotPresent` (default) \| `Never`.
- `pod.replicas.<service>` — integer 1..5 (default 1) for each service of that component
  (e.g. `keystone.pod.replicas.api`, `heat.pod.replicas.{api,cfn,engine}`,
  `designate.pod.replicas.{api,central,mdns,producer,sink,worker}`).

Notable per-component fields:

- **keystone**
  - `images.tags.keystone_api` — full image ref for the keystone-api container (e.g. a
    build that ships `mod_auth_openidc` for OIDC federation).
  - `network.api.node_port.{enabled,port}` — expose the API on a NodePort (30000..32767,
    default 30500); OIDC redirects run through the browser, so Keystone must be reachable at
    a public HTTPS hostname.
  - `conf.keystone.{auth.methods,openid.remote_id_attribute,federation,auto_provisioning}`,
    `conf.software.apache2.a2enmod`, `conf.wsgi_keystone` — OIDC federation overrides.
    Free-form subtrees (`federation`, `wsgi_keystone`) carry preserve-unknown-fields because
    the upstream OSH conf tree does not round-trip cleanly through crdgen.
- **horizon**
  - `network.dashboard.service.type` — `NodePort` (default) \| `LoadBalancer` \| `ClusterIP`.
  - `network.dashboard.service.annotations` — string map for cloud LB controls.
  - `network.node_port.{enabled,port}` — NodePort (default enabled, 31000).
  - `conf.horizon.local_settings.config.auth` — WebSSO overrides (preserve-unknown-fields).
- **neutron** — `network.interface.tunnel` — host NIC used as the VXLAN tunnel endpoint
  (e.g. `ens4` on GKE/GCE; default `ens4`).
- **ironic** (bare metal)
  - `pod.replicas.{api,conductor}`.
  - `labels.conductor.{node_selector_key,node_selector_value}` — the conductor uses
    `hostNetwork` for PXE/TFTP/HTTP-boot and must land on a node attached to the provisioning
    network (default `openstack-control-plane=enabled`).
  - `network.pxe.{device,neutron_provider_network,neutron_subnet_cidr,neutron_subnet_gateway,neutron_subnet_alloc_start,neutron_subnet_alloc_end}`
    — the provisioning network (default NIC `ironic-pxe`, provider network `ironic`, CIDR
    `172.24.6.0/24`).

Set `spec: {}` (or omit `componentValues.<name>`) to use the validated defaults.

## `serviceContract` (contract v1)

The service integration descriptor. Chart defaults are authoritative; deployments override
only environment-specific fields (URLs, realm). All top-level keys are required:
`version`, `service`, `identity`, `roleMapping`, `capabilities`, `sso`, `usage`, `health`,
`tagging`, `ticketing`.

| Key | Shape | Purpose |
| --- | --- | --- |
| `version` | `"v1"` (const) | Contract version. |
| `service` | `{name, displayName, description?, owner?}` | DNS-1123 identifier + display metadata. |
| `identity.keycloak` | `{client, groupsMapper, idpBroker?}` | Keycloak client (`clientId`, `realmRef?`, `protocol`), groups mapper (`claim`, `fullPath`), optional IdP broker (`alias`, `providerRef?`). |
| `roleMapping[]` | `{cmpRole, nativeRole, scope?, description?}` | **Mandatory**, min 1 — maps CMP roles/capabilities to native OpenStack roles. |
| `capabilities[]` | see [api](./api.md) | Atomic permissions; each renders an aggregation-labeled ClusterRole, ABAC ones additionally a Kyverno ClusterPolicy. |
| `sso` | `{deepLink, logout}` | Context deep-link (`urlTemplate` with `{org}/{tenant}/{namespace}/{project}`, optional `preRedirect`) + logout (`oidc-backchannel` default, or `saml-slo`). |
| `usage` | `{interval, metrics[]}` | UsageRecord emission declaration; each metric `{name, unit, kind: gauge\|counter\|capacity, dimensions?}`. |
| `health` | `{interval, source}` | Normalized health emission mapped to `OK\|Warning\|Critical\|Unknown`; `source.type` is `endpoint`\|`metric`\|`adapter`. |
| `tagging` | `{native, remediation?, keyPattern?, maxTagsPerResource?}` | Tag support declaration. |
| `ticketing` | `{category, subcategory?, routing?}` | Support-ticket classification and routing hints. |

The full JSON schema (including the `capability` and `policyRule` definitions and the
conditional `allOf`/`if`/`then` constraints) is documented in [api](./api.md).

## `contractEmitter` (disabled by default)

The runtime side of `serviceContract.usage`/`health`: CronJobs polling the OpenStack APIs on
the declared intervals and writing normalized `UsageRecord` / health rows to ClickHouse over
its HTTP interface. `additionalProperties: false`.

| Field | Type | Purpose |
| --- | --- | --- |
| `enabled` | boolean (default `false`) | Turn the CronJobs on. |
| `image` / `imagePullPolicy` | string | Emitter image + pull policy. |
| `org` | string | Organization stamped on every emitted record (platform-injected). |
| `openstack` | `{existingSecret, authUrl?, region?, interface?, tlsVerify?}` | OpenStack-Helm admin openrc secret (`OS_*` keys); `interface` is `internal`\|`public`\|`admin`. |
| `clickhouse` | `{url, database, usageTable, healthTable, bootstrapHealthTable?, existingSecret?}` | ClickHouse HTTP target + table names; optional secret with `username`/`password`. |
| `resources` | object | Container requests/limits. |

See [contract-emitter](./contract-emitter.md) for the emission model and record shapes.

## Invariants

- **One namespace per installation** — every Composition of one install shares a namespace;
  cross-component references resolve by fixed Service DNS name.
- **Deterministic + hook-free** — each blueprint chart renders byte-identically on repeat and
  emits no Helm hooks; CI enforces both (see [release](./release.md)). Do not reintroduce
  `randAlphaNum`/`uuidv4`/`now` into a rendered manifest.
- **`componentValues` schema in sync** — regenerate with
  `python3 hack/gen-componentvalues-schema.py` after any component schema change and commit
  the result, or CI fails.
