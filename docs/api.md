---
type: API
title: krateo-openstack-blueprint — API
description: The CompositionDefinition CRD each blueprint registers, the per-component composition Kinds, and the Openstack orchestrator composition schema (profile/enabled/componentValues/serviceContract/contractEmitter).
resource: oci://ghcr.io/krateo-blueprints/charts/openstack
tags: [openstack, api, compositiondefinition, crd, service-contract]
timestamp: 2026-08-11T00:00:00Z
---

# API

The blueprints expose two layers of API: the **`CompositionDefinition`** each blueprint
registers (which is what makes a component's composition Kind available), and the
**composition Kinds** themselves (the CRDs Krateo derives from each chart's
`values.schema.json`).

## The `CompositionDefinition` CRD

Each blueprint ships a sibling `compositiondefinition.yaml`. Applying it registers the
blueprint's chart with Krateo; the core-provider then generates a CRD (the composition Kind)
from that chart's `values.schema.json` and reconciles Compositions of it.

```yaml
apiVersion: core.krateo.io/v1alpha1
kind: CompositionDefinition
metadata:
  name: keystone
  namespace: openstack-system
spec:
  chart:
    url: oci://ghcr.io/krateo-blueprints/charts/keystone
    version: "0.3.1"
```

| Field | Type | Purpose |
| --- | --- | --- |
| `spec.chart.url` | string | OCI reference to the published blueprint chart (`oci://ghcr.io/krateo-blueprints/charts/<component>`). |
| `spec.chart.version` | string | The exact published chart version to register. |

Applying `blueprints/<c>/compositiondefinition.yaml` for a component makes that component's
composition Kind available. The orchestrator does this for you when `installDefinitions:
true` (the default).

## Per-component composition Kinds

Every component chart is named after its OpenStack service, so its derived Kind is the
PascalCase of that name in `composition.krateo.io/v0-2-0`:

| Kind | Component | Tier |
| --- | --- | --- |
| `Mariadb` | Relational datastore | identity |
| `Memcached` | Token/catalog cache | identity |
| `Keystone` | Identity (the core) | identity |
| `Glance` | Image service | identity |
| `Horizon` | Dashboard (web UI) | identity |
| `Rabbitmq` | Message bus | compute |
| `Placement` | Resource placement | compute |
| `Openvswitch` | OVS datapath agent | compute |
| `Libvirt` | libvirt/QEMU hypervisor | compute |
| `Nova` | Compute (VMs, QEMU) | compute |
| `Neutron` | Networking (ML2/OVS, VXLAN) | compute |
| `Ironic` | Bare-metal provisioning | compute |
| `Cinder` | Block storage | compute |
| `Heat` | Orchestration (stacks) | compute |
| `Barbican` | Key manager | compute |
| `Designate` | DNS service | compute |
| `Octavia` | Load balancing (LBaaS) | compute |
| `Magnum` | Container infra (k8s) | compute |
| `Manila` | Shared filesystems | compute |
| `Mistral` | Workflow service | compute |
| `Ceilometer` | Telemetry collection | compute |
| `Aodh` | Alarming service | compute |
| `Cloudkitty` | Rating/chargeback | compute |
| `Masakari` | Instances HA | compute |
| `Trove` | Database as a service | compute |
| `Watcher` | Resource optimization | compute |
| `Tacker` | NFV orchestration | compute |
| `Cyborg` | Accelerator management | compute |
| `Blazar` | Resource reservation | compute |
| `Zaqar` | Messaging service | compute |
| `Freezer` | Backup as a service | compute |
| `Skyline` | Modern dashboard | compute |
| `Gnocchi` | Metrics storage | compute |
| `Swift` | Object storage | compute |
| `Openstack` | Orchestrator (sequences the above) | umbrella |

Each component Composition's `spec` is the curated chart values for that component (see
[configuration](./configuration.md#componentvaluesname--per-component-overrides) for the
common shapes). `spec: {}` uses the validated defaults.

```yaml
apiVersion: composition.krateo.io/v0-2-0
kind: Keystone
metadata:
  name: keystone
  namespace: openstack
spec: {}
```

## The `Openstack` orchestrator composition

The umbrella composition (Kind `Openstack`) is the recommended entry point. Its `spec` is
typed by `blueprints/openstack/chart/values.schema.json`:

| Field | Type | Default | Purpose |
| --- | --- | --- | --- |
| `profile` | string (`identity`\|`full`) | `identity` | Component preset. |
| `enabled` | array\|null | `[]` | Explicit component set (overrides `profile`), expanded to the transitive dependency closure. |
| `installDefinitions` | boolean | `true` | Register each component `CompositionDefinition` first. |
| `ociRepo` | string | `oci://ghcr.io/krateo-blueprints/charts` | Registry for the component charts. |
| `chartVersion` | string | `0.3.2` | Component chart version to pin. |
| `componentValues` | object\|null | `null` | Per-component `spec` overrides, strictly typed. |
| `serviceContract` | object | chart default | Contract v1 descriptor (below). |
| `contractEmitter` | object | disabled | Runtime usage/health CronJobs. |

## `serviceContract` schema (contract v1)

Required top-level keys: `version` (const `v1`), `service`, `identity`, `roleMapping`,
`capabilities`, `sso`, `usage`, `health`, `tagging`, `ticketing`.
`additionalProperties: false` throughout.

### `capability` (referenced by `capabilities[]`)

An atomic, human-readable permission. Required: `name` (DNS-1123, ≤63), `title`,
`enforcement`, `rules`.

| Field | Type | Purpose |
| --- | --- | --- |
| `enforcement` | `rbac`\|`acr`\|`cross-org`\|`abac` | `rbac` = Kubernetes RBAC only; `acr` = auth-strength/step-up on top of RBAC; `cross-org` = central parent-realm role; `abac` = attribute/conditional rule (Kyverno CEL) on top of RBAC. |
| `requiresAcr` | string | ACR/LoA the caller's token must satisfy. **Required when `enforcement: acr`.** |
| `crossOrgRole` | string | Central parent-realm role. **Required when `enforcement: cross-org`.** |
| `aggregateTo[]` | string[] | Predefined roles this capability aggregates into (rendered as `rbac.krateo.io/aggregate-to-<role>` labels). |
| `rules[]` | `policyRule` | RBAC rules (see below), min 1. |
| `abac` | `{celExpression, message?, match.kinds[]}` | Kyverno ClusterPolicy (CEL). **Required when `enforcement: abac`.** |

### `policyRule` (referenced by `capability.rules[]`)

Required: `apiGroups[]`, `resources[]`, `verbs[]`. Optional: `resourceNames[]`. `verbs`
enum: `get`, `list`, `watch`, `create`, `update`, `patch`, `delete`, `deletecollection`, `*`.

### Conditional constraints

- `sso.deepLink.preRedirect.type ∈ {token-rescope, custom}` ⇒ `endpoint` required.
- `sso.logout.mode = oidc-backchannel` ⇒ `backchannelLogoutUrl` required;
  `mode = saml-slo` ⇒ `sloUrl` required.
- `health.source.type = endpoint` ⇒ `source.endpoint` (HTTPS) required.

The full field surface (including `usage.metrics[]`, `health`, `tagging`, `ticketing`) is
tabulated in [configuration](./configuration.md#servicecontract-contract-v1); the runtime
emitter that satisfies the `usage`/`health` declaration is documented in
[contract-emitter](./contract-emitter.md).
