---
type: Configuration
title: krateo-openstack-blueprint — configuration
description: The whole Openstack orchestrator value surface — profile, enabled, installDefinitions, ociRepo, componentValues (typed by each component's CRD), the serviceContract stanza, and the contractEmitter CronJobs.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack
tags: [openstack, configuration, values, service-contract, contract-emitter]
timestamp: 2026-08-11T00:00:00Z
---

# Configuration

Everything below is the `spec` of an `Openstack` Composition (Kind `Openstack`,
`composition.krateo.io/v<chart-version-with-dashes>`), fully typed by the orchestrator chart's
`blueprints/openstack/chart/values.schema.json`. The per-component blueprints have their
own, smaller schemas; the orchestrator's `componentValues` mirrors them.

## Orchestrator top-level

| Field | Type | Default | Purpose |
| --- | --- | --- | --- |
| `profile` | string (`identity` \| `full`) | `identity` | `identity` = MariaDB+Memcached+Keystone+Glance+Horizon; `full` = also RabbitMQ+Placement+OVS+libvirt+Neutron+Nova (compute; needs an amd64 cluster with the OVS kernel module). |
| `enabled` | array of string \| null | `[]` | Deploy exactly these components plus their transitive dependencies, **overriding** `profile`. E.g. `["heat"]` also brings up keystone, rabbitmq, mariadb and memcached. Empty uses `profile`. |
| `installDefinitions` | boolean | `true` | Register each component blueprint's `CompositionDefinition` as the first step. |
| `ociRepo` | string | `oci://ghcr.io/krateo-blueprints/charts` | Registry path holding the per-component blueprint charts. They are pulled at the orchestrator's own chart version. Up to 0.3.37 a `chartVersion` value chose it, and its schema default was frozen into every Composition at create time, so upgrades left the components behind (#179). A stale `chartVersion` in an existing Composition is accepted and ignored. |
| `componentValues` | object \| null | `null` | Per-component Composition `spec` overrides (see below). |
| `serviceContract` | object | chart default | Service integration descriptor, contract v1 (see below). |
| `contractEmitter` | object | disabled | Runtime usage/health emission CronJobs (see below). |

## `componentValues.<name>` — per-component overrides

Per-component Composition `spec` overrides, keyed by component name. The value at
`componentValues.<name>` becomes that component's Composition `spec`, deep-merged; components
without an entry use chart defaults. `additionalProperties: false` applies to the component
**names**: an unknown component is rejected.

The umbrella passes each `componentValues.<name>` through as an open object, and the component's
**own Composition CRD** types and validates it, from `blueprints/<name>/chart/values.schema.json`.
The umbrella used to embed every component schema here; that validated nothing the component CRD
does not, and it multiplied the umbrella CRD's size by every version the CRD still serves (#209).
`hack/gen-componentvalues-schema.py` generates this shape (CI fails a stale one), and
`hack/umbrella-crd-size` fails CI if the generated umbrella CRD, times 4 served versions, would
exceed 1 MiB.

Each component exposes **every top-level key of its chart's `values.yaml`**, not a chosen subset:
`conf`, `pod`, `manifests`, `endpoints`, `network`, `images`, `labels`, `bootstrap`, `extraObjects`,
`jobs`, `secrets` and the rest. Declared fields under them are typed (`manifests.*` as booleans,
`labels.*.node_selector_{key,value}` as strings, `extraObjects` as a list of manifests, top-level
scalars by their default's type); any other key below them passes through untyped
(`x-kubernetes-preserve-unknown-fields`) instead of being pruned.
`hack/validate-component-schema-surface.py` fails CI if a chart grows a top-level key its schema
does not declare, if a subtree under one is closed, or if a declared scalar type does not accept
the chart's own default (#209). Up to 0.3.44 most of these keys were not exposed, and an override
of them was silently pruned by the Composition CRD.

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
  - `oidc.clientSecretRef.{name,key}` and `oidc.cryptoPassphraseRef.{name,key}` — the Secrets
    holding mod_auth_openidc's client secret and its crypto passphrase (the key for its state and
    session cookies). Never put `OIDCClientSecret` or `OIDCCryptoPassphrase` in
    `conf.wsgi_keystone` (or `conf.security`, `conf.mpm_event`): values end up in the Composition
    CR, so the chart refuses to render either. With the references set, keystone-api reads
    `OIDC_CLIENT_SECRET` and `OIDC_CRYPTO_PASSPHRASE` from those Secrets and the chart renders
    `OIDCClientSecret ${OIDC_CLIENT_SECRET}` and `OIDCCryptoPassphrase ${OIDC_CRYPTO_PASSPHRASE}`
    in its own Apache file, which every VirtualHost inherits. Set each reference's name and key
    together, or neither.

    The passphrase is required whenever OIDC is configured (a `clientSecretRef`, or any OIDC
    directive in `conf.wsgi_keystone`): mod_auth_openidc 2.4.11 refuses to start without it and
    `apache2 -t` does not notice, so the chart fails the render instead of letting keystone-api
    crash-loop. Every keystone-api replica must use the same passphrase.

    Example, with both values generated once by External Secrets Operator Password generators in
    the release namespace:

    ```yaml
    apiVersion: generators.external-secrets.io/v1alpha1
    kind: Password
    metadata:
      name: keystone-oidc
      namespace: openstack
    spec:
      length: 48
      symbols: 0
      noUpper: false
      allowRepeat: true
    ---
    apiVersion: external-secrets.io/v1
    kind: ExternalSecret
    metadata:
      name: keystone-oidc-client
      namespace: openstack
    spec:
      refreshInterval: "0"
      target:
        name: keystone-oidc-client
      dataFrom:
        - sourceRef:
            generatorRef:
              apiVersion: generators.external-secrets.io/v1alpha1
              kind: Password
              name: keystone-oidc
          rewrite:
            - regexp:
                source: "^password$"
                target: client-secret
    ---
    apiVersion: external-secrets.io/v1
    kind: ExternalSecret
    metadata:
      name: keystone-oidc-crypto
      namespace: openstack
    spec:
      refreshInterval: "0"
      target:
        name: keystone-oidc-crypto
      dataFrom:
        - sourceRef:
            generatorRef:
              apiVersion: generators.external-secrets.io/v1alpha1
              kind: Password
              name: keystone-oidc
          rewrite:
            - regexp:
                source: "^password$"
                target: passphrase
    ```

    Each ExternalSecret runs the generator once and gets its own value. This example has not yet
    been applied to a live External Secrets Operator. In the Keystone spec:

    ```yaml
    oidc:
      clientSecretRef:
        name: keystone-oidc-client
        key: client-secret
      cryptoPassphraseRef:
        name: keystone-oidc-crypto
        key: passphrase
    ```

    **Keycloak reads the same Secret.** With keycloak-config-kog 0.4.9 or later, the
    KeycloakClient for Keystone lives in the same `openstack` namespace and points
    `spec.clientSecretRef` at `keystone-oidc-client` / `client-secret`. That keeps one generated
    value with no copy step. Never set the `krateo.io/connector-verbose` annotation on that
    KeycloakClient: Keycloak's client-list response carries confidential client secrets, and
    verbose dumps are not redacted.

    **Rotating the client secret takes two steps.**
    1. keystone-api reads `OIDC_CLIENT_SECRET` only when its container starts, so after the Secret
       changes, roll it: `kubectl -n openstack rollout restart deployment/keystone-api`. The chart
       cannot roll it for you on a Secret change: that needs a checksum of the Secret's value in
       the pod template, which means the render reading the secret, and this chart deliberately
       never does.
    2. Keycloak gets the new value only when the KeycloakClient's spec changes or the CR is
       recreated. A changed Secret alone is not drift for keycloak-config-kog. Trigger one of
       those in the same change.

    Until both steps are done, Keystone and Keycloak disagree and logins fail. Rotating the crypto
    passphrase needs only the keystone-api rollout, but it invalidates every state and session
    cookie made with the old one, so users in a login flow at that moment have to start again.
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
